"""Tests for session-expiry detection and the one-shot re-login retry.

Gradescope's server-side session eventually expires while the process still
holds the cookie. ``auth`` installs a response hook that turns the expiry
signals (redirect to /login, the login page itself, 401 "must be logged in")
into ``SessionExpiredError``, and ``with_session_recovery`` — applied to every
MCP tool and resource — re-runs the call once on a fresh login.

The fake Gradescope below sits behind ``HTTPAdapter.send`` and models each
login as a separate session (identified by the CSRF token the login page
hands out), so expiring "the session" leaves later logins valid.
"""

from __future__ import annotations

import functools
import inspect
import json
import threading
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
import requests
from mcp.server.mcpserver import MCPServer
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from gradescope_mcp import auth, server

BASE = "https://www.gradescope.com"
EMAIL = "prof@example.edu"
PASSWORD = "Hunter2-Secret!"

HOME_PAGE = (
    '<html><form action="/login" method="post">'
    '<input type="hidden" name="authenticity_token" value="AUTH-TOKEN"></form></html>'
)
LOGIN_PAGE = (
    '<html><head><meta name="csrf-token" content="LOGIN-PAGE-CSRF"></head><body>'
    '<form action="/login" method="post">'
    '<input type="hidden" name="authenticity_token" value="AUTH-TOKEN">'
    '<input name="session[email]"><input name="session[password]"></form></body></html>'
)


def _account_page(csrf: str) -> str:
    """Account page with one instructor course, parseable by gradescopeapi."""
    return (
        f'<html><head><meta name="csrf-token" content="{csrf}"></head><body>'
        '<button class="js-createNewCourse">Create</button>'
        '<div id="account-show">'
        '<h2 class="pageHeading">Instructor Courses</h2>'
        '<div class="courseList"><div class="courseList--term">Fall 2026</div>'
        '<div class="courseList--coursesForTerm">'
        '<a href="/courses/123"><h3 class="courseBox--shortname">CS 101</h3>'
        '<div class="courseBox--name">Intro to Testing</div>'
        '<div class="courseBox--assignments">3 assignments</div></a>'
        "</div></div></div></body></html>"
    )


def make_response(request, status: int, body: str = "", headers: dict | None = None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode()
    resp.headers = CaseInsensitiveDict(headers or {})
    resp.url = request.url
    resp.request = request
    resp.encoding = "utf-8"
    return resp


MUST_LOG_IN = json.dumps({"error": "You must be logged in to access this page."})
NOT_AUTHORIZED = json.dumps({"error": "You are not authorized to access this page."})


class FakeGradescope:
    """Offline Gradescope with per-login sessions that can be expired."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.logins = 0
        self.valid: set[str] = set()
        self.expiry_mode = "redirect"  # or "401"
        self.sessions_die_immediately = False
        self.routes: dict[str, object] = {}
        self._landing: str | None = None
        self._lock = threading.Lock()

    def expire_all(self) -> None:
        self.valid.clear()

    def _expired(self, request):
        if self.expiry_mode == "401":
            return make_response(
                request, 401, MUST_LOG_IN, {"Content-Type": "application/json"}
            )
        return make_response(request, 302, "", {"Location": BASE + "/login"})

    def handle(self, request):
        with self._lock:
            self.sent.append((request.method, urlsplit(request.url).path or "/"))
        path = urlsplit(request.url).path or "/"
        if path == "/":
            return make_response(request, 200, HOME_PAGE)
        if path == "/login" and request.method == "POST":
            body = request.body.decode() if isinstance(request.body, bytes) else request.body
            form = parse_qs(body or "")
            if form.get("session[password]") == [PASSWORD]:
                with self._lock:
                    self.logins += 1
                    csrf = f"CSRF-{self.logins}"
                    if not self.sessions_die_immediately:
                        self.valid.add(csrf)
                    self._landing = csrf
                return make_response(request, 302, "", {"Location": BASE + "/account"})
            return make_response(request, 200, LOGIN_PAGE)
        if path == "/login":
            return make_response(request, 200, LOGIN_PAGE)

        token = request.headers.get("X-CSRF-Token")
        if path == "/logout":
            self.valid.discard(token)  # logging out ends that session
            return make_response(request, 302, "", {"Location": BASE + "/"})
        if token is None and path == "/account" and self._landing is not None:
            csrf, self._landing = self._landing, None
            return make_response(request, 200, _account_page(csrf))
        if token not in self.valid:
            return self._expired(request)
        route = self.routes.get(path)
        if route is not None:
            return route(request)
        if path == "/account":
            return make_response(request, 200, _account_page(token))
        return make_response(request, 200, f"<html>page {path}</html>")


@pytest.fixture(autouse=True)
def _clean_auth_state(monkeypatch):
    monkeypatch.setattr(auth, "_connection", None)
    monkeypatch.setattr(auth, "_failed_login", None)
    auth._local.expired = None
    auth._local.recovering = False
    monkeypatch.setenv("GRADESCOPE_EMAIL", EMAIL)
    monkeypatch.setenv("GRADESCOPE_PASSWORD", PASSWORD)
    monkeypatch.delenv(auth.HTTP_TIMEOUT_ENV, raising=False)
    yield
    auth._local.expired = None
    auth._local.recovering = False


@pytest.fixture
def fake_gs(monkeypatch) -> FakeGradescope:
    fake = FakeGradescope()

    def send(adapter, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        return fake.handle(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return fake


def _fetch(path: str) -> str:
    """A tool-shaped function: swallows every error into a string."""
    try:
        conn = auth.get_connection()
        return conn.session.get(BASE + path).text
    except auth.AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error: {e}"


# ------------------------------------------------------------------
# Response hook
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [BASE + "/login", "/login", "/login?return_to=%2Fcourses", "/account/auth"],
)
def test_hook_raises_on_redirect_to_login_before_following_it(fake_gs, location) -> None:
    conn = auth.get_connection()
    fake_gs.routes["/courses/1"] = lambda req: make_response(req, 302, "", {"Location": location})
    fake_gs.sent.clear()

    with pytest.raises(auth.SessionExpiredError, match="session expired"):
        conn.session.get(BASE + "/courses/1")

    assert fake_gs.sent == [("GET", "/courses/1")]  # the login page was not fetched
    assert auth._local.expired is conn


def test_hook_raises_on_redirected_write_without_resending_it_as_get(fake_gs) -> None:
    conn = auth.get_connection()
    fake_gs.expire_all()
    fake_gs.sent.clear()

    with pytest.raises(auth.SessionExpiredError):
        conn.session.post(BASE + "/courses/1/questions/2/rubric_items", data={"x": "1"})

    # requests would have turned the POST into GET /login; the hook stops it.
    assert fake_gs.sent == [("POST", "/courses/1/questions/2/rubric_items")]


@pytest.mark.parametrize(
    "body",
    [MUST_LOG_IN, '{"error": "YOU MUST BE LOGGED IN to access this page."}'],
)
def test_hook_raises_on_401_must_be_logged_in(fake_gs, body) -> None:
    conn = auth.get_connection()
    fake_gs.routes["/courses/1/answer_groups"] = lambda req: make_response(req, 401, body)

    with pytest.raises(auth.SessionExpiredError, match="must be logged in"):
        conn.session.get(BASE + "/courses/1/answer_groups")
    assert auth._local.expired is conn


def test_hook_ignores_permission_401_and_unrelated_redirects(fake_gs) -> None:
    conn = auth.get_connection()
    fake_gs.routes["/courses/1/extensions"] = lambda req: make_response(req, 401, NOT_AUTHORIZED)
    fake_gs.routes["/courses/1/old"] = lambda req: make_response(
        req, 302, "", {"Location": "/courses/1/new"}
    )
    fake_gs.routes["/courses/1/sso"] = lambda req: make_response(
        req, 302, "", {"Location": "https://sso.example.edu/login"}
    )
    fake_gs.routes["/courses/1/loginish"] = lambda req: make_response(
        req, 302, "", {"Location": "/courses/1/login_history"}
    )

    assert conn.session.get(BASE + "/courses/1/extensions").status_code == 401
    assert conn.session.get(BASE + "/courses/1/old").url == BASE + "/courses/1/new"
    assert conn.session.get(BASE + "/courses/1/sso", allow_redirects=False).status_code == 302
    assert conn.session.get(BASE + "/courses/1/loginish").status_code == 200

    assert conn.logged_in is True
    assert auth._local.expired is None


def test_hook_raises_when_the_login_page_is_served(fake_gs) -> None:
    conn = auth.get_connection()
    with pytest.raises(auth.SessionExpiredError, match="login page"):
        conn.session.get(BASE + "/login")


def test_login_flow_is_not_treated_as_expiry(fake_gs, monkeypatch) -> None:
    """The login flow visits /login legitimately; it never trips the hook."""
    monkeypatch.setenv("GRADESCOPE_PASSWORD", "wrong")
    with pytest.raises(auth.AuthError) as info:
        auth.get_connection()
    assert not isinstance(info.value, auth.SessionExpiredError)
    assert str(info.value) == "Gradescope login failed: invalid credentials."

    monkeypatch.setenv("GRADESCOPE_PASSWORD", PASSWORD)
    first = auth.get_connection()
    assert auth._local.expired is None
    fake_gs.expire_all()
    assert _fetch("/courses/1").startswith("Authentication error: Gradescope session expired")
    assert auth._local.expired is first

    # Logging in again (which visits /login) on a fresh connection is clean.
    auth._local.expired = None
    auth.reset_connection(first)
    second = auth.get_connection()
    assert second is not first
    assert fake_gs.logins == 2
    assert auth._local.expired is None
    assert second.session.get(BASE + "/courses/1").status_code == 200


def test_valid_session_is_not_flagged(fake_gs) -> None:
    auth.get_connection()
    assert _fetch("/courses/1") == "<html>page /courses/1</html>"
    assert auth._local.expired is None


# ------------------------------------------------------------------
# with_session_recovery
# ------------------------------------------------------------------


def test_recovery_reruns_once_when_the_tool_swallowed_the_expiry(fake_gs) -> None:
    calls = []

    @auth.with_session_recovery
    def tool(path: str) -> str:
        calls.append(path)
        return _fetch(path)

    auth.get_connection()
    fake_gs.expire_all()

    assert tool("/courses/1") == "<html>page /courses/1</html>"
    assert calls == ["/courses/1", "/courses/1"]
    assert fake_gs.logins == 2


@pytest.mark.parametrize("mode", ["redirect", "401"])
def test_recovery_returns_clear_message_when_expiry_repeats(fake_gs, mode) -> None:
    fake_gs.expiry_mode = mode
    fake_gs.sessions_die_immediately = True
    calls = []

    @auth.with_session_recovery
    def tool() -> str:
        calls.append(1)
        return _fetch("/courses/1")

    result = tool()

    assert result == (
        "Authentication error: Gradescope session expired and re-login did not restore access."
    )
    assert result == auth.SESSION_RECOVERY_FAILED_MESSAGE
    assert len(calls) == 2  # exactly one retry
    assert fake_gs.logins == 2


def test_recovery_raises_clear_error_when_the_tool_does_not_swallow(fake_gs) -> None:
    fake_gs.sessions_die_immediately = True
    calls = []

    @auth.with_session_recovery
    def tool() -> str:
        calls.append(1)
        return auth.get_connection().session.get(BASE + "/courses/1").text

    with pytest.raises(auth.SessionExpiredError, match="re-login did not restore access"):
        tool()
    assert len(calls) == 2


def test_recovery_reruns_when_the_tool_raised_the_expiry(fake_gs) -> None:
    calls = []

    @auth.with_session_recovery
    def tool() -> str:
        calls.append(1)
        return auth.get_connection().session.get(BASE + "/courses/1").text

    auth.get_connection()
    fake_gs.expire_all()
    assert tool() == "<html>page /courses/1</html>"
    assert len(calls) == 2


def test_recovery_does_not_retry_without_expiry(monkeypatch) -> None:
    resets = []
    monkeypatch.setattr(auth, "reset_connection", lambda *a, **k: resets.append(a))
    calls = []

    @auth.with_session_recovery
    def failing_read() -> str:
        calls.append(1)
        return "Error: Unable to access roster (status 403)."

    @auth.with_session_recovery
    def crashing() -> str:
        calls.append(1)
        raise ValueError("boom")

    assert failing_read() == "Error: Unable to access roster (status 403)."
    with pytest.raises(ValueError, match="boom"):
        crashing()
    assert len(calls) == 2
    assert resets == []


def test_recovery_passes_login_failures_through(fake_gs, monkeypatch) -> None:
    monkeypatch.setenv("GRADESCOPE_PASSWORD", "wrong")
    calls = []

    @auth.with_session_recovery
    def tool() -> str:
        calls.append(1)
        return _fetch("/courses/1")

    assert tool() == "Authentication error: Gradescope login failed: invalid credentials."
    assert len(calls) == 1


def test_recovery_preserves_function_metadata() -> None:
    def tool_example(course_id: str, confirm_write: bool = False) -> str:
        """Docstring the MCP tool description comes from."""
        return course_id

    wrapped = auth.with_session_recovery(tool_example)

    assert wrapped.__name__ == "tool_example"
    assert wrapped.__doc__ == tool_example.__doc__
    assert inspect.signature(wrapped) == inspect.signature(tool_example)
    assert wrapped.__wrapped__ is tool_example
    assert wrapped("1") == "1"


def test_recovery_rejects_async_functions() -> None:
    async def tool() -> str:
        return ""

    with pytest.raises(TypeError, match="sync"):
        auth.with_session_recovery(tool)


def test_a_call_logs_in_again_at_most_once(fake_gs) -> None:
    """A tool that keeps going after an expiry (per-row errors in a batch)
    must not log in again for every row, even if new sessions keep dying."""
    fake_gs.sessions_die_immediately = True

    @auth.with_session_recovery
    def batch() -> str:
        return "\n".join(_fetch(f"/rows/{i}") for i in range(5))

    assert batch() == auth.SESSION_RECOVERY_FAILED_MESSAGE
    assert fake_gs.logins == 2  # first attempt + the single retry


def test_nested_wrappers_retry_only_once(fake_gs) -> None:
    fake_gs.sessions_die_immediately = True
    inner_calls = []

    @auth.with_session_recovery
    def inner() -> str:
        inner_calls.append(1)
        return _fetch("/courses/1")

    @auth.with_session_recovery
    def outer() -> str:
        return inner()

    assert outer() == auth.SESSION_RECOVERY_FAILED_MESSAGE
    assert len(inner_calls) == 2


def test_nested_wrapper_does_not_hide_an_outer_expiry(fake_gs) -> None:
    auth.get_connection()
    fake_gs.expire_all()
    outer_calls = []

    @auth.with_session_recovery
    def inner() -> str:
        return "inner"

    @auth.with_session_recovery
    def outer() -> str:
        outer_calls.append(1)
        page = _fetch("/courses/1")  # expires on the first run
        inner()
        return page

    assert outer() == "<html>page /courses/1</html>"
    assert len(outer_calls) == 2


def test_expiry_flag_is_thread_local(fake_gs) -> None:
    """Another thread's expiry must not make this thread's call re-run."""
    conn = auth.get_connection()
    fake_gs.routes["/expired"] = lambda req: make_response(
        req, 302, "", {"Location": BASE + "/login"}
    )
    other_thread_expired = threading.Event()
    calls = []

    def other_thread() -> None:
        with pytest.raises(auth.SessionExpiredError):
            conn.session.get(BASE + "/expired")
        other_thread_expired.set()

    @auth.with_session_recovery
    def tool() -> str:
        calls.append(1)
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()
        assert other_thread_expired.is_set()
        return "done"

    assert tool() == "done"
    assert calls == [1]


def test_reset_inside_a_recovered_call_does_not_deadlock(fake_gs) -> None:
    calls = []
    outcome = {}

    @auth.with_session_recovery
    def tool() -> str:
        calls.append(1)
        result = _fetch("/courses/1")
        auth.reset_connection()  # a tool resetting the connection itself
        return result

    auth.get_connection()
    fake_gs.expire_all()

    worker = threading.Thread(target=lambda: outcome.setdefault("result", tool()))
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "reset_connection deadlocked inside a recovered call"
    assert outcome["result"] == "<html>page /courses/1</html>"
    assert len(calls) == 2


def test_concurrent_expiries_keep_the_fresh_connection(fake_gs) -> None:
    """Two calls that saw the same expiry must not drop each other's re-login.

    Order forced: A and B both expire on the old connection; A resets and
    logs in again; only then does B reset. B's reset must keep A's fresh
    connection (without the guard it would log it out under A's feet).
    """
    auth.get_connection()
    fake_gs.expire_all()
    holding = threading.Barrier(2)
    a_logged_in_again = threading.Event()
    b_retrying = threading.Event()
    role = threading.local()
    results = {}

    @auth.with_session_recovery
    def tool() -> str:
        retry = getattr(role, "attempted", False)
        role.attempted = True
        conn = auth.get_connection()
        if not retry:
            holding.wait(timeout=5)  # both hold the old, already-expired connection
        elif role.name == "A":
            a_logged_in_again.set()
            b_retrying.wait(timeout=5)  # B has reset by now
        else:
            b_retrying.set()
        try:
            text = conn.session.get(BASE + "/courses/1").text
        except auth.SessionExpiredError as e:
            text = f"Authentication error: {e}"
        if not retry and role.name == "B":
            a_logged_in_again.wait(timeout=5)  # B resets only after A's re-login
        return text

    def run(name: str) -> None:
        role.name = name
        results[name] = tool()

    threads = [threading.Thread(target=run, args=(name,)) for name in "AB"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results == {"A": "<html>page /courses/1</html>", "B": "<html>page /courses/1</html>"}
    assert fake_gs.logins == 2  # the initial login plus ONE shared re-login


# ------------------------------------------------------------------
# MCP layer
# ------------------------------------------------------------------


def test_mcp_tool_call_recovers_from_one_expiry(fake_gs) -> None:
    first = anyio.run(server.mcp.call_tool, "tool_list_courses", {})
    assert first.is_error is False
    assert "CS 101" in first.content[0].text
    assert fake_gs.logins == 1

    fake_gs.expire_all()
    second = anyio.run(server.mcp.call_tool, "tool_list_courses", {})

    assert second.is_error is False
    assert "CS 101" in second.content[0].text
    assert "expired" not in second.content[0].text
    assert fake_gs.logins == 2


def test_mcp_tool_call_reports_failed_recovery(fake_gs) -> None:
    fake_gs.sessions_die_immediately = True

    result = anyio.run(server.mcp.call_tool, "tool_list_courses", {})

    assert result.content[0].text == auth.SESSION_RECOVERY_FAILED_MESSAGE
    assert fake_gs.logins == 2


def test_mcp_resource_read_recovers_from_one_expiry(fake_gs) -> None:
    auth.get_connection()
    fake_gs.expire_all()

    async def read():
        return list(await server.mcp.read_resource("gradescope://courses"))

    contents = anyio.run(read)

    assert "CS 101" in contents[0].content
    assert fake_gs.logins == 2


def test_every_tool_and_resource_is_registered_with_recovery() -> None:
    for tool in server.mcp._tool_manager.list_tools():
        assert tool.fn is getattr(server, tool.name)
        assert inspect.unwrap(tool.fn) is not tool.fn, tool.name
    for resource in server.mcp._resource_manager.list_resources():
        assert inspect.unwrap(resource.fn) is not resource.fn, resource.uri
    for template in server.mcp._resource_manager.list_templates():
        assert inspect.unwrap(template.fn) is not template.fn, template.uri_template


def test_recovery_wrappers_keep_registered_metadata_identical() -> None:
    """Registering the unwrapped functions yields byte-identical listings.

    Tools are registered with the same annotations, title and text-only
    output as the server's ``gs_tool`` uses, so any difference comes from
    the wrappers.
    """
    baseline = MCPServer("baseline")
    for tool in server.mcp._tool_manager.list_tools():
        baseline.tool(
            title=tool.title, annotations=tool.annotations, structured_output=False
        )(inspect.unwrap(tool.fn))
    for resource in server.mcp._resource_manager.list_resources():
        baseline.resource(str(resource.uri))(inspect.unwrap(resource.fn))
    for template in server.mcp._resource_manager.list_templates():
        baseline.resource(template.uri_template)(inspect.unwrap(template.fn))

    async def listing(mcp_server):
        return (
            [t.model_dump(mode="json", by_alias=True) for t in await mcp_server.list_tools()],
            [r.model_dump(mode="json", by_alias=True) for r in await mcp_server.list_resources()],
            [t.model_dump(mode="json", by_alias=True) for t in await mcp_server.list_resource_templates()],
        )

    actual = anyio.run(listing, server.mcp)
    expected = anyio.run(listing, baseline)

    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert len(actual[0]) == 38
    by_name = {t["name"]: t for t in actual[0]}
    assert by_name["tool_apply_grade"]["inputSchema"]["required"] == [
        "course_id", "question_id", "submission_id",
    ]


def test_prompts_are_registered_without_recovery() -> None:
    """Prompts only build text; they make no Gradescope requests."""
    prompts = server.mcp._prompt_manager.list_prompts()
    assert len(prompts) == 7
    for prompt in prompts:
        assert not hasattr(getattr(server, prompt.name), "__wrapped__"), prompt.name
