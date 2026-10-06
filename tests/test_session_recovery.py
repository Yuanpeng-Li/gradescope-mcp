"""Tests for session-expiry detection and the one-shot re-login retry.

Gradescope's server-side session eventually expires while the process still
holds the cookie. ``auth`` installs a response hook that turns the expiry
signals (redirect to /login, the login page itself, 401 "must be logged in",
the logged-out home page with the login form) into ``SessionExpiredError``,
and ``with_session_recovery`` — applied to every MCP tool and resource —
re-runs the call once on a fresh login, unless Gradescope had already
accepted a write during the call.

The fake Gradescope below sits behind ``HTTPAdapter.send`` and models each
login as a separate session (identified by the CSRF token the login page
hands out), so expiring "the session" leaves later logins valid.
"""

from __future__ import annotations

import functools
import html
import inspect
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
import requests
from mcp.server.mcpserver import MCPServer
from requests.adapters import BaseAdapter, HTTPAdapter
from requests.structures import CaseInsensitiveDict

from gradescope_mcp import auth, server
from gradescope_mcp.tools import answer_groups, grading_ops

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
        '<nav><a href="/logout">Log Out</a></nav>'
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
    """Offline Gradescope with per-login sessions that can be expired.

    ``expiry_mode`` is how a rejected session is answered: ``"redirect"``
    (302 to /login), ``"401"`` (401 "must be logged in") or ``"root"`` (302
    to /, the logged-out home page with the login form).
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.logins = 0
        self.valid: set[str] = set()
        self.expiry_mode = "redirect"
        self.sessions_die_immediately = False
        self.routes: dict[str, object] = {}
        self.home_content_type: str | None = "text/html; charset=utf-8"
        self._landing: str | None = None
        self._lock = threading.Lock()

    def expire_all(self) -> None:
        self.valid.clear()

    def writes(self) -> list[tuple[str, str]]:
        """Requests other than GET/HEAD, without the login POSTs."""
        return [(m, p) for m, p in self.sent if m not in ("GET", "HEAD") and p != "/login"]

    def _expired(self, request):
        if self.expiry_mode == "401":
            return make_response(
                request, 401, MUST_LOG_IN, {"Content-Type": "application/json"}
            )
        target = "/" if self.expiry_mode == "root" else "/login"
        return make_response(request, 302, "", {"Location": BASE + target})

    def handle(self, request):
        with self._lock:
            self.sent.append((request.method, urlsplit(request.url).path or "/"))
        path = urlsplit(request.url).path or "/"
        if path == "/":
            if request.headers.get("X-CSRF-Token") in self.valid:
                # A logged-in session is sent on to its account page.
                return make_response(request, 302, "", {"Location": BASE + "/account"})
            headers = {"Content-Type": self.home_content_type} if self.home_content_type else {}
            return make_response(request, 200, HOME_PAGE, headers)
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
    auth._reset_call_state()
    auth._local.recovering = False
    monkeypatch.setenv("GRADESCOPE_EMAIL", EMAIL)
    monkeypatch.setenv("GRADESCOPE_PASSWORD", PASSWORD)
    monkeypatch.delenv(auth.HTTP_TIMEOUT_ENV, raising=False)
    yield
    auth._reset_call_state()
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
    assert str(info.value).startswith("Gradescope login failed: invalid credentials.")

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

    # The error comes first (so the result is an error), followed by the
    # first attempt's own output instead of discarding it.
    assert result.startswith(
        "Authentication error: Gradescope session expired and re-login did not restore access.\n\n"
        "Output of the first attempt (the session had expired, so it may be "
        "incomplete or wrong):\nAuthentication error: Gradescope session expired ("
    )
    assert result.startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
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

    assert tool().startswith("Authentication error: Gradescope login failed: invalid credentials.")
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

    assert batch().startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
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

    assert outer().startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
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

    assert result.is_error is True
    assert result.content[0].text.startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
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
    assert len(actual[0]) == 39
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


def _call_tool(name: str, args: dict) -> tuple[bool, str]:
    result = anyio.run(server.mcp.call_tool, name, args)
    return result.is_error, "\n".join(c.text for c in result.content)


# ------------------------------------------------------------------
# An expired session sent to the logged-out home page
# ------------------------------------------------------------------


@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", None])
def test_hook_flags_a_redirect_to_the_logged_out_home_page(fake_gs, content_type) -> None:
    fake_gs.expiry_mode = "root"
    fake_gs.home_content_type = content_type
    conn = auth.get_connection()
    fake_gs.expire_all()
    fake_gs.sent.clear()

    with pytest.raises(auth.SessionExpiredError, match="logged-out page"):
        conn.session.get(BASE + "/courses/1/assignments")

    assert fake_gs.sent == [("GET", "/courses/1/assignments"), ("GET", "/")]
    assert auth._local.expired is conn


def test_write_redirected_to_the_home_page_is_an_expiry_not_a_write(fake_gs) -> None:
    """requests turns the redirected POST into GET /; the hook stops there,
    and the POST is not counted as an accepted write."""
    fake_gs.expiry_mode = "root"
    conn = auth.get_connection()
    fake_gs.expire_all()
    fake_gs.sent.clear()

    with pytest.raises(auth.SessionExpiredError):
        conn.session.post(BASE + "/courses/1/assignments/2", data={"title": "HW3"})

    assert fake_gs.sent == [("POST", "/courses/1/assignments/2"), ("GET", "/")]
    assert auth._writes_this_call() == 0


def test_logged_in_pages_with_a_login_form_or_non_html_are_not_expiry(fake_gs) -> None:
    conn = auth.get_connection()
    with_logout = HOME_PAGE.replace("<html>", '<html><a href="/logout">Log Out</a>')
    fake_gs.routes["/courses/1/sso"] = lambda req: make_response(
        req, 200, with_logout, {"Content-Type": "text/html"}
    )
    fake_gs.routes["/courses/1/data.json"] = lambda req: make_response(
        req, 200, json.dumps({"html": HOME_PAGE}), {"Content-Type": "application/json"}
    )
    escaped = f'<div data-react-props="{html.escape(HOME_PAGE, quote=True)}"></div>'
    fake_gs.routes["/courses/1/grade"] = lambda req: make_response(
        req, 200, escaped, {"Content-Type": "text/html"}
    )
    fake_gs.routes["/courses/1/stream"] = lambda req: make_response(req, 200, HOME_PAGE)

    assert conn.session.get(BASE + "/courses/1/sso").status_code == 200
    assert conn.session.get(BASE + "/courses/1/data.json").status_code == 200
    assert conn.session.get(BASE + "/courses/1/grade").status_code == 200
    # A streamed body without a Content-Type is not read by the hook.
    assert conn.session.get(BASE + "/courses/1/stream", stream=True).status_code == 200
    # A logged-in session sent to "/" lands on its account page.
    assert conn.session.get(BASE + "/").url == BASE + "/account"
    assert auth._local.expired is None


@pytest.mark.parametrize(
    "name, args",
    [
        ("tool_rename_assignment", {"new_title": "HW3"}),
        ("tool_update_autograder_image", {"image_name": "gradescope/autograder-base:latest"}),
        ("tool_upload_submission", {}),
    ],
)
def test_writes_through_upstream_helpers_do_not_report_success_after_root_expiry(
    fake_gs, tmp_path, name, args
) -> None:
    """Reviewer repros v23_root.py / upload_success.py: every session is sent
    to "/". The tools used to report '✅ renamed', '✅ image set' and
    '✅ Submission uploaded ... URL: https://www.gradescope.com/'."""
    fake_gs.expiry_mode = "root"
    fake_gs.sessions_die_immediately = True
    if name == "tool_upload_submission":
        upload = tmp_path / "hw.pdf"
        upload.write_bytes(b"%PDF-1.4 answer")
        args = {"file_paths": [str(upload)]}
    args = {"course_id": "1", "assignment_id": "2", "confirm_write": True, **args}

    is_error, text = _call_tool(name, args)

    assert is_error is True
    assert text.startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
    assert "✅" not in text
    assert fake_gs.writes() == []  # each helper stopped at its first (GET) request
    assert fake_gs.logins == 2  # the one re-login


@pytest.mark.parametrize(
    "name, args",
    [
        ("tool_get_assignments", {"course_id": "1"}),
        ("tool_list_question_submissions", {"course_id": "1", "question_id": "2"}),
    ],
)
def test_reads_report_the_root_expiry_instead_of_an_empty_result(fake_gs, name, args) -> None:
    fake_gs.expiry_mode = "root"
    fake_gs.sessions_die_immediately = True

    is_error, text = _call_tool(name, args)

    assert is_error is True
    assert text.startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
    assert fake_gs.logins == 2


def test_root_expiry_is_recovered_by_one_re_login(fake_gs) -> None:
    fake_gs.expiry_mode = "root"
    first_is_error, first = _call_tool("tool_list_courses", {})
    assert first_is_error is False and "CS 101" in first

    fake_gs.expire_all()
    is_error, text = _call_tool("tool_list_courses", {})

    assert is_error is False
    assert "CS 101" in text and "expired" not in text
    assert fake_gs.logins == 2


# ------------------------------------------------------------------
# A call during which Gradescope accepted a write is not re-run
# ------------------------------------------------------------------


def _expire_after_first(fake: FakeGradescope, answer):
    """A route that answers, and lets the session die right after its first answer."""
    state = {"done": False}

    def route(request):
        response = answer(request)
        if not state["done"]:
            state["done"] = True
            fake.expire_all()
        return response

    return route


def _save_then_read_back(runs: list):
    @auth.with_session_recovery
    def tool() -> str:
        runs.append(1)
        session = auth.get_connection().session
        try:
            session.post(BASE + "/courses/1/save", data={"x": "1"})
            session.get(BASE + "/courses/1/readback")
        except auth.AuthError as e:
            return f"Authentication error: {e}"
        return "done"

    return tool


@pytest.mark.parametrize("status", [200, 201, 204])
def test_accepted_write_stops_the_re_run(fake_gs, status) -> None:
    fake_gs.routes["/courses/1/save"] = _expire_after_first(
        fake_gs, lambda req: make_response(req, status, "")
    )
    runs: list = []

    result = _save_then_read_back(runs)()

    assert runs == [1]  # not re-run
    assert result == (
        "Authentication error: Gradescope session expired (redirected to the login page).\n\n"
        + auth._writes_then_expiry_notice(1)
    )
    assert "accepted 1 write request(s)" in result
    assert fake_gs.writes() == [("POST", "/courses/1/save")]
    # The expired connection was dropped: the next call logs in first.
    assert auth._connection is None
    assert fake_gs.logins == 1


def test_redirected_write_that_lands_on_a_page_counts(fake_gs) -> None:
    fake_gs.routes["/courses/1/save"] = lambda req: make_response(
        req, 302, "", {"Location": BASE + "/courses/1/saved"}
    )
    fake_gs.routes["/courses/1/saved"] = _expire_after_first(
        fake_gs, lambda req: make_response(req, 200, "<html>saved</html>")
    )
    runs: list = []

    result = _save_then_read_back(runs)()

    assert runs == [1]
    assert "accepted 1 write request(s)" in result


def test_rejected_write_is_re_run(fake_gs) -> None:
    fake_gs.routes["/courses/1/save"] = _expire_after_first(
        fake_gs, lambda req: make_response(req, 422, "invalid")
    )
    runs: list = []

    assert _save_then_read_back(runs)() == "done"
    assert len(runs) == 2


@pytest.mark.parametrize("mode", ["redirect", "root", "401"])
def test_write_answered_by_the_expired_session_is_re_run(fake_gs, mode) -> None:
    """The expired session turned the POST away (a redirect to /login, to the
    logged-out home page, or a 401): it was not processed, so the call is re-run."""
    fake_gs.expiry_mode = mode
    fake_gs.routes["/courses/1/save"] = lambda req: make_response(req, 200, "{}")
    auth.get_connection()
    fake_gs.expire_all()
    runs: list = []

    assert _save_then_read_back(runs)() == "done"
    assert len(runs) == 2
    assert fake_gs.logins == 2


def test_tool_that_raised_after_a_write_reports_it(fake_gs) -> None:
    fake_gs.routes["/courses/1/save"] = _expire_after_first(
        fake_gs, lambda req: make_response(req, 200, "{}")
    )
    runs = []

    @auth.with_session_recovery
    def tool() -> str:
        runs.append(1)
        session = auth.get_connection().session
        session.post(BASE + "/courses/1/save", data={"x": "1"})
        return session.get(BASE + "/courses/1/readback").text  # raises

    with pytest.raises(auth.SessionExpiredError, match="accepted 1 write request"):
        tool()
    assert runs == [1]


def test_retry_that_writes_and_expires_again_keeps_its_report(fake_gs) -> None:
    """No write in the first run; the re-run saves, then expires again."""
    fake_gs.routes["/courses/1/save"] = lambda req: (
        fake_gs.expire_all() or make_response(req, 200, "{}")
    )
    runs = []

    @auth.with_session_recovery
    def tool() -> str:
        runs.append(1)
        session = auth.get_connection().session
        try:
            session.get(BASE + "/courses/1/form")
            session.post(BASE + "/courses/1/save", data={"x": "1"})
            session.get(BASE + "/courses/1/readback")
        except auth.AuthError as e:
            return f"saved: {len(fake_gs.writes())}; then {e}"
        return "all done"

    auth.get_connection()
    fake_gs.expire_all()

    result = tool()

    assert len(runs) == 2
    assert result.startswith("saved: 1; then Gradescope session expired")
    assert result.endswith(auth._writes_then_expiry_notice(1))


def test_double_expiry_without_writes_keeps_the_first_output(fake_gs) -> None:
    """The first attempt's output follows the error instead of being
    discarded, and a plausible-looking empty result is still an error."""
    fake_gs.sessions_die_immediately = True
    outputs = iter(["No assignments found in run 1.", "run 2 output"])

    @auth.with_session_recovery
    def tool() -> str:
        try:
            auth.get_connection().session.get(BASE + "/courses/1/assignments")
        except auth.AuthError:
            pass
        return next(outputs)

    result = tool()

    assert result == (
        f"{auth.SESSION_RECOVERY_FAILED_MESSAGE}\n\n"
        "Output of the first attempt (the session had expired, so it may be "
        "incomplete or wrong):\nNo assignments found in run 1."
    )
    assert server.is_error_text(result)


# --- reviewer repro R1/expiry_rerun.py: the tools themselves ----------------


class _RubricServer:
    """Grading pages with a rubric and an answer group; each session expires
    right after its first write."""

    def __init__(self) -> None:
        self.rubric = [
            {"id": 777, "description": "Missing units", "weight": 2.0},
            {"id": 778, "description": "Ok", "weight": 0},
        ]
        self.valid: set[str] = set()
        self.writes: list[tuple[str, str]] = []
        self.groups = {
            "groups": [{"id": 3, "title": "g"}],
            "submissions": [
                {"id": 101, "confirmed_group_id": 3, "graded": False},
                {"id": 102, "confirmed_group_id": 3, "graded": False},
            ],
        }
        self.connections = 0

    def page(self) -> str:
        props = {
            "question": {"weight": 10, "scoring_type": "negative"},
            "submission": {"id": 5, "score": None, "graded": False},
            "evaluation": {},
            "rubric_items": self.rubric,
            "rubric_item_evaluations": [],
            "urls": {"save_grade": "/courses/1/questions/2/submissions/5/save_grade"},
        }
        return (
            '<html><head><meta name="csrf-token" content="tok"></head><body>'
            '<a href="/courses/1/questions/2/submissions/5/grade">x</a>'
            '<div data-react-class="SubmissionGrader" data-react-props="'
            f'{html.escape(json.dumps(props), quote=True)}"></div></body></html>'
        )


class _RubricAdapter(BaseAdapter):
    def __init__(self, state: _RubricServer, sid: str) -> None:
        super().__init__()
        self.state, self.sid = state, sid

    def send(self, request, **kwargs):
        path = request.url.replace(BASE, "")
        if self.sid not in self.state.valid:
            return make_response(request, 302, "", {"Location": BASE + "/login"})
        if request.method == "GET" and path.endswith("/answer_groups"):
            return make_response(
                request, 200, json.dumps(self.state.groups), {"Content-Type": "application/json"}
            )
        if request.method == "GET":
            return make_response(request, 200, self.state.page(), {"Content-Type": "text/html"})
        if request.method == "DELETE":
            item_id = int(path.rsplit("/", 1)[1])
            self.state.rubric = [x for x in self.state.rubric if x["id"] != item_id]
            self.state.writes.append(("DELETE", path))
            self.state.valid.discard(self.sid)
            return make_response(request, 204, "")
        if request.method == "POST" and path.endswith("/save_many_grades"):
            for member in self.state.groups["submissions"]:
                member["graded"] = True
            self.state.writes.append(("POST", path))
            self.state.valid.discard(self.sid)
            return make_response(
                request, 200, '{"ok":true}', {"Content-Type": "application/json"}
            )
        return make_response(request, 404, "not found")

    def close(self) -> None:
        pass


@pytest.fixture
def rubric_server(monkeypatch) -> _RubricServer:
    state = _RubricServer()
    current: list = [None]

    def get_conn():
        if current[0] is None:
            state.connections += 1
            sid = f"s{state.connections}"
            state.valid.add(sid)
            session = requests.Session()
            session.mount("https://", _RubricAdapter(state, sid))
            conn = SimpleNamespace(gradescope_base_url=BASE, session=session, logged_in=True)
            auth._install_expiry_hook(conn)
            current[0] = conn
        return current[0]

    monkeypatch.setattr(auth, "reset_connection", lambda expired=None: current.__setitem__(0, None))
    monkeypatch.setattr(grading_ops, "get_connection", get_conn)
    monkeypatch.setattr(answer_groups, "get_connection", get_conn)
    return state


def test_delete_rubric_item_is_not_reported_as_unchanged_after_expiry(rubric_server) -> None:
    """The DELETE succeeded and the read-back expired. The re-run used to
    answer "rubric item `777` is not in question `2`'s rubric. Nothing was
    changed." with isError."""
    is_error, text = _call_tool(
        "tool_delete_rubric_item",
        {"course_id": "1", "question_id": "2", "rubric_item_id": "777", "confirm_write": True},
    )

    assert is_error is False
    assert "Nothing was changed" not in text and "is not in question" not in text
    assert "Rubric item `777` deleted" in text
    assert "accepted 1 write request(s)" in text
    assert rubric_server.writes == [("DELETE", "/courses/1/questions/2/rubric_items/777")]
    assert [x["id"] for x in rubric_server.rubric] == [778]
    assert rubric_server.connections == 1  # not re-run on a second session


def test_grade_answer_group_is_not_reported_as_unsent_after_expiry(rubric_server) -> None:
    """save_many_grades succeeded and the read-back expired. The re-run used
    to answer "already has 2 graded member(s) ... Nothing was sent"."""
    is_error, text = _call_tool(
        "tool_grade_answer_group",
        {
            "course_id": "1", "question_id": "2", "group_id": "3",
            "rubric_item_ids": ["778"], "confirm_write": True, "expected_member_count": 2,
        },
    )

    assert is_error is False
    assert "Nothing was sent" not in text and "already has" not in text
    assert "Batch grade saved" in text
    assert "accepted 1 write request(s)" in text
    assert rubric_server.writes == [
        ("POST", "/courses/1/questions/2/submissions/5/save_many_grades")
    ]
    assert [m["graded"] for m in rubric_server.groups["submissions"]] == [True, True]
    assert rubric_server.connections == 1


# --- reviewer repro R2/double_expiry.py: saved grades stay visible ----------

_GRADING_RUBRIC = [
    {"id": 7, "description": "Correct", "weight": 0},
    {"id": 8, "description": "Wrong", "weight": 5},
]


class _GradingServer:
    """Each login's session survives a fixed number of authenticated requests."""

    def __init__(self, lifetimes: list[int]) -> None:
        self.lifetimes = lifetimes
        self.logins = 0
        self.landing: str | None = None
        self.budget: dict[str, int] = {}
        self.saved: dict[str, dict] = {}
        self.session_token: str | None = None

    def grader(self, sid: str) -> str:
        saved = self.saved.get(sid)
        applied = [
            int(k) for k, v in (saved or {}).get("rubric_items", {}).items()
            if v.get("score") == "true"
        ]
        props = {
            "question": {"id": 2, "title": "1", "weight": 5, "scoring_type": "negative"},
            "submission": {
                "id": int(sid), "owner_names": "S", "score": None,
                "graded": bool(saved), "answers": {},
            },
            "evaluation": (saved or {}).get(
                "question_submission_evaluation", {"points": None, "comments": None}
            ),
            "rubric_items": _GRADING_RUBRIC,
            "rubric_item_evaluations": [{"rubric_item_id": r, "present": True} for r in applied],
            "urls": {"save_grade": f"/courses/1/questions/2/submissions/{sid}/save_grade"},
            "navigation_urls": {},
        }
        return (
            '<html><head><meta name="csrf-token" content="C"></head><body>'
            '<div data-react-class="SubmissionGrader" data-react-props="'
            + html.escape(json.dumps(props), quote=True)
            + '"></div></body></html>'
        )

    def handle(self, request):
        path = urlsplit(request.url).path or "/"
        if path == "/":
            return make_response(request, 200, HOME_PAGE)
        if path == "/login" and request.method == "POST":
            self.logins += 1
            token = f"T{self.logins}"
            self.landing = token
            self.budget[token] = self.lifetimes[min(self.logins - 1, len(self.lifetimes) - 1)]
            return make_response(request, 302, "", {"Location": BASE + "/account"})
        if path == "/account" and self.landing:
            token, self.landing = self.landing, None
            return make_response(
                request, 200, f'<html><head><meta name="csrf-token" content="{token}"></head></html>'
            )
        # Writes carry the grading page's token ("C"); identify the session
        # by its session-wide header instead.
        token = request.headers.get("X-CSRF-Token")
        token = self.session_token if token == "C" else token
        if self.budget.get(token, 0) <= 0:
            return make_response(request, 302, "", {"Location": BASE + "/login"})
        self.budget[token] -= 1
        if path.endswith("/grade"):
            return make_response(
                request, 200, self.grader(path.split("/")[-2]), {"Content-Type": "text/html"}
            )
        if path.endswith("/save_grade") and request.method == "POST":
            self.saved[path.split("/")[-2]] = json.loads(request.body)
            return make_response(request, 200, "{}", {"Content-Type": "application/json"})
        return make_response(request, 404, "not found")


@pytest.fixture
def grading_server(monkeypatch):
    def install(lifetimes: list[int]) -> _GradingServer:
        state = _GradingServer(lifetimes)
        original = requests.Session.send

        def session_send(session, request, **kwargs):
            state.session_token = session.headers.get("X-CSRF-Token")
            return original(session, request, **kwargs)

        def adapter_send(adapter, request, stream=False, timeout=None, verify=True,
                         cert=None, proxies=None):
            return state.handle(request)

        monkeypatch.setattr(requests.Session, "send", session_send)
        monkeypatch.setattr(HTTPAdapter, "send", adapter_send)
        return state

    return install


def test_batch_that_saved_rows_before_the_expiry_reports_them(grading_server) -> None:
    """Rows 5 and 6 are saved, then the session dies (and so would the
    re-login's). The result used to be a bare 'Authentication error: ...
    re-login did not restore access.'"""
    state = grading_server([7, 1])
    grades = [{"submission_id": s, "rubric_item_ids": ["8"]} for s in ("5", "6", "9")]

    _, text = _call_tool(
        "tool_apply_grade_batch",
        {"course_id": "1", "question_id": "2", "grades": grades, "confirm_write": True},
    )

    assert sorted(state.saved) == ["5", "6"]
    assert not text.startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
    assert "### Saved" in text and "`5`" in text and "`6`" in text
    assert "accepted 2 write request(s)" in text
    assert state.logins == 1  # not re-run, so rows 5 and 6 were not re-sent


def test_single_grade_saved_before_the_expiry_is_reported(grading_server) -> None:
    state = grading_server([2, 0])

    _, text = _call_tool(
        "tool_apply_grade",
        {
            "course_id": "1", "question_id": "2", "submission_id": "5",
            "rubric_item_ids": ["8"], "confirm_write": True,
        },
    )

    assert sorted(state.saved) == ["5"]
    assert not text.startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
    assert "Grade saved" in text
    assert "accepted 1 write request(s)" in text
    assert state.logins == 1
