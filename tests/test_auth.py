"""Tests for the auth module: login, credential hygiene, cooldown, timeouts.

The login runs against an offline fake Gradescope installed behind
``requests.adapters.HTTPAdapter.send``. Everything above the transport — the
real ``requests.Session``, the timeout adapter, redirects, gradescopeapi's
``Account`` — is the production code path. Session-expiry detection and
recovery are covered in ``test_session_recovery.py``.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import pytest
import requests
from gradescopeapi.classes.account import Account
from gradescopeapi.classes.connection import GSConnection
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from gradescope_mcp import auth

BASE = "https://www.gradescope.com"
EMAIL = "prof@example.edu"
# Characters that change under URL/escape encoding, to exercise scrubbing.
PASSWORD = "Hunter2-Secret!&x=1"

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
    return (
        f'<html><head><meta name="csrf-token" content="{csrf}"></head>'
        '<body><div id="account-show"></div></body></html>'
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


class FakeGradescope:
    """Offline Gradescope login flow; records (method, url, body, timeout)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, object]] = []
        self.logins = 0
        self.login_status: int | None = None  # force an HTTP status on POST /login
        self.login_delay = 0.0
        self._lock = threading.Lock()
        self._landing_csrf: str | None = None

    def handle(self, request, timeout):
        body = request.body or ""
        if isinstance(body, bytes):
            body = body.decode()
        with self._lock:
            self.sent.append((request.method, request.url, body, timeout))
        path = urlsplit(request.url).path or "/"
        if path == "/":
            return make_response(request, 200, HOME_PAGE)
        if path == "/login" and request.method == "POST":
            time.sleep(self.login_delay)
            if self.login_status is not None:
                return make_response(request, self.login_status, "<html>error</html>")
            form = parse_qs(body)
            if (
                form.get("session[email]") == [EMAIL]
                and form.get("session[password]") == [PASSWORD]
                and form.get("authenticity_token") == ["AUTH-TOKEN"]
            ):
                with self._lock:
                    self.logins += 1
                    self._landing_csrf = f"CSRF-{self.logins}"
                return make_response(request, 302, "", {"Location": BASE + "/account"})
            return make_response(request, 200, LOGIN_PAGE)
        if path == "/login":
            return make_response(request, 200, LOGIN_PAGE)
        if path == "/logout":
            return make_response(request, 302, "", {"Location": BASE + "/"})
        if path == "/account":
            csrf = request.headers.get("X-CSRF-Token") or self._landing_csrf or "NONE"
            return make_response(request, 200, _account_page(csrf))
        return make_response(request, 200, "<html>ok</html>")


@pytest.fixture(autouse=True)
def _clean_auth_state(monkeypatch):
    monkeypatch.setattr(auth, "_connection", None)
    monkeypatch.setattr(auth, "_failed_login", None)
    auth._local.expired = None
    monkeypatch.setenv("GRADESCOPE_EMAIL", EMAIL)
    monkeypatch.setenv("GRADESCOPE_PASSWORD", PASSWORD)
    monkeypatch.delenv(auth.HTTP_TIMEOUT_ENV, raising=False)
    yield
    auth._local.expired = None


@pytest.fixture
def fake_gs(monkeypatch) -> FakeGradescope:
    fake = FakeGradescope()

    def send(adapter, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        return fake.handle(request, timeout)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return fake


def _block_sockets(monkeypatch) -> None:
    """Make every real connection attempt fail in the connect phase."""

    def blocked(*args, **kwargs):
        raise OSError(101, "network access blocked by test")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


def _secret_forms() -> list[str]:
    return [
        PASSWORD,
        quote(PASSWORD),
        quote(PASSWORD, safe=""),
        quote_plus(PASSWORD),
        EMAIL,
        quote_plus(EMAIL),
    ]


# ------------------------------------------------------------------
# Login
# ------------------------------------------------------------------


def test_login_sends_credentials_as_form_body_not_url(fake_gs) -> None:
    conn = auth.get_connection()

    posts = [entry for entry in fake_gs.sent if entry[0] == "POST"]
    assert len(posts) == 1
    _, url, body, _ = posts[0]
    assert url == BASE + "/login"
    form = parse_qs(body)
    assert form["session[email]"] == [EMAIL]
    assert form["session[password]"] == [PASSWORD]
    assert form["authenticity_token"] == ["AUTH-TOKEN"]
    for _, sent_url, _, _ in fake_gs.sent:
        assert "session" not in urlsplit(sent_url).query
        assert all(secret not in sent_url for secret in _secret_forms())

    assert isinstance(conn, GSConnection)
    assert conn.logged_in is True
    assert conn.session.headers["X-CSRF-Token"] == "CSRF-1"
    assert isinstance(conn.account, Account)
    assert conn.account.session is conn.session
    assert auth.get_connection() is conn
    assert fake_gs.logins == 1


def test_missing_credentials_raise_auth_error(monkeypatch, fake_gs) -> None:
    monkeypatch.delenv("GRADESCOPE_PASSWORD")
    with pytest.raises(auth.AuthError, match="Missing Gradescope credentials"):
        auth.get_connection()
    assert fake_gs.sent == []


def test_invalid_credentials_are_not_retried_until_env_changes(monkeypatch, fake_gs) -> None:
    monkeypatch.setenv("GRADESCOPE_PASSWORD", "wrong-password")

    with pytest.raises(auth.AuthError) as first:
        auth.get_connection()
    assert str(first.value) == "Gradescope login failed: invalid credentials."
    attempts = len(fake_gs.sent)

    # Cooldown: same credentials -> same error, Gradescope is not contacted.
    for _ in range(3):
        with pytest.raises(auth.AuthError) as again:
            auth.get_connection()
        assert str(again.value) == "Gradescope login failed: invalid credentials."
    assert len(fake_gs.sent) == attempts

    # Changed credentials are tried again.
    monkeypatch.setenv("GRADESCOPE_PASSWORD", PASSWORD)
    conn = auth.get_connection()
    assert conn.logged_in is True
    assert fake_gs.logins == 1
    assert auth._failed_login is None


@pytest.mark.parametrize("status", [500, 503, 429])
def test_server_errors_during_login_are_not_cached(fake_gs, status) -> None:
    fake_gs.login_status = status
    with pytest.raises(auth.AuthError, match=f"HTTP {status}"):
        auth.get_connection()
    assert auth._failed_login is None

    fake_gs.login_status = None
    assert auth.get_connection().logged_in is True


def test_missing_login_form_is_not_cached(monkeypatch, fake_gs) -> None:
    original = fake_gs.handle

    def no_form(request, timeout):
        if urlsplit(request.url).path in ("", "/"):
            return make_response(request, 503, "<html>maintenance</html>")
        return original(request, timeout)

    monkeypatch.setattr(fake_gs, "handle", no_form)
    with pytest.raises(auth.AuthError, match="login form was not found"):
        auth.get_connection()
    assert auth._failed_login is None


def test_connect_failure_during_login_does_not_leak_credentials(monkeypatch, caplog) -> None:
    """GET / succeeds, then POST /login fails while connecting, through the
    real requests -> urllib3 stack (sockets blocked)."""
    real_send = HTTPAdapter.send
    posted_urls = []

    def send(adapter, request, **kwargs):
        if request.method == "GET" and urlsplit(request.url).path in ("", "/"):
            return make_response(request, 200, HOME_PAGE)
        posted_urls.append(request.url)
        return real_send(adapter, request, **kwargs)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    _block_sockets(monkeypatch)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(auth.AuthError) as info:
        auth.get_connection()

    err = info.value
    message = str(err)
    assert posted_urls == [BASE + "/login"]
    assert message.startswith("Gradescope login failed: network error")
    assert "ConnectionError" in message
    for secret in _secret_forms():
        assert secret not in message
        assert secret not in repr(err)
        assert secret not in caplog.text
    # The unscrubbed original exception is not chained onto the AuthError.
    assert err.__cause__ is None and err.__suppress_context__
    # Network failures are not cached as bad credentials.
    assert auth._failed_login is None


def test_failure_reason_scrubs_query_string_credentials(monkeypatch) -> None:
    """Defense in depth: even an upstream-style query-string login error
    (gradescopeapi's ``params=`` login) is scrubbed by the reason formatter."""
    _block_sockets(monkeypatch)
    with pytest.raises(requests.ConnectionError) as info:
        requests.Session().post(
            BASE + "/login",
            params={"session[email]": EMAIL, "session[password]": PASSWORD},
        )
    assert quote_plus(PASSWORD) in str(info.value)  # the leak being guarded against

    reason = auth._describe_failure(info.value, EMAIL, PASSWORD)

    assert reason.startswith("ConnectionError: ")
    assert "session%5Bpassword%5D=[REDACTED]" in reason
    for secret in _secret_forms():
        assert secret not in reason


def test_scrub_redacts_raw_escaped_and_query_forms() -> None:
    text = (
        f"user={EMAIL} pw={PASSWORD!r} url=/login?session[password]=other-secret&x "
        f"enc={quote_plus(PASSWORD)} lower={quote(PASSWORD, safe='').lower()} "
        f"raw=/login?session[email]={EMAIL}&session[password]={PASSWORD}&commit=1"
    )
    scrubbed = auth._scrub(text, EMAIL, PASSWORD)
    assert "other-secret" not in scrubbed
    assert "session[password]=[REDACTED]" in scrubbed
    # The raw password contains "&x=1"; no fragment of it may survive.
    assert "&x=1" not in scrubbed
    assert scrubbed.endswith("raw=/login?session[email]=[REDACTED]&session[password]=[REDACTED]&commit=1")
    for secret in _secret_forms() + [quote(PASSWORD, safe="").lower()]:
        assert secret not in scrubbed


# ------------------------------------------------------------------
# Timeouts
# ------------------------------------------------------------------


def test_default_timeout_reaches_the_adapter(fake_gs) -> None:
    conn = auth.get_connection()

    stock = HTTPAdapter()
    for prefix in ("https://", "http://"):
        adapter = conn.session.get_adapter(prefix + "www.gradescope.com/")
        assert isinstance(adapter, auth.TimeoutHTTPAdapter)
        # Pooling and retries are the stock HTTPAdapter's.
        assert adapter._pool_connections == stock._pool_connections
        assert adapter._pool_maxsize == stock._pool_maxsize
        assert adapter._pool_block == stock._pool_block
        assert adapter.max_retries.total == stock.max_retries.total
    assert conn.session.get_adapter("https://x/") is not conn.session.get_adapter("http://x/")

    # The login flow itself ran with the default timeout.
    assert {timeout for *_, timeout in fake_gs.sent} == {(10.0, 60.0)}

    fake_gs.sent.clear()
    conn.account.get_courses()  # gradescopeapi helper on the same session
    conn.session.get(BASE + "/courses/1", timeout=5)
    assert [timeout for *_, timeout in fake_gs.sent] == [(10.0, 60.0), 5]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("120", (10.0, 120.0)),
        ("3", (3.0, 3.0)),
        ("2.5", (2.5, 2.5)),
        ("abc", (10.0, 60.0)),
        ("0", (10.0, 60.0)),
        ("-5", (10.0, 60.0)),
        ("nan", (10.0, 60.0)),
        ("inf", (10.0, 60.0)),
    ],
)
def test_timeout_env_override(monkeypatch, fake_gs, raw, expected) -> None:
    monkeypatch.setenv(auth.HTTP_TIMEOUT_ENV, raw)
    conn = auth.get_connection()
    fake_gs.sent.clear()
    conn.session.get(BASE + "/courses/1")
    assert fake_gs.sent[-1][3] == expected


# ------------------------------------------------------------------
# reset_connection
# ------------------------------------------------------------------


def test_reset_connection_invokes_logout_when_available() -> None:
    """If the cached connection exposes logout(), reset calls it best-effort."""
    logout_called = {"n": 0}

    class Conn:
        logged_in = True
        def logout(self):
            logout_called["n"] += 1

    auth._connection = Conn()
    auth.reset_connection()
    assert logout_called["n"] == 1
    assert auth._connection is None


def test_reset_connection_swallows_logout_failures() -> None:
    """A failing upstream logout() must not stop us from clearing local state."""
    class Conn:
        logged_in = True
        def logout(self):
            raise RuntimeError("upstream broke")

    auth._connection = Conn()
    auth.reset_connection()
    assert auth._connection is None


def test_reset_connection_skips_logout_for_an_expired_session() -> None:
    """A session Gradescope already rejected has nothing to log out."""
    calls = []

    class Conn:
        logged_in = True
        def logout(self):
            calls.append(1)

    conn = Conn()
    auth._connection = conn
    auth.reset_connection(conn)
    assert calls == []
    assert auth._connection is None


def test_reset_connection_with_expected_keeps_a_newer_connection() -> None:
    old, new = object(), type("Conn", (), {"logged_in": True})()
    auth._connection = new
    auth.reset_connection(old)
    assert auth._connection is new


def test_reset_logout_does_not_trip_the_expiry_hook(fake_gs, monkeypatch) -> None:
    """Gradescope's /logout redirect must not be mistaken for an expiry."""
    original = fake_gs.handle
    logouts = []

    def logout_to_login(request, timeout):
        if urlsplit(request.url).path == "/logout":
            logouts.append(request.url)
            return make_response(request, 302, "", {"Location": BASE + "/login"})
        return original(request, timeout)

    monkeypatch.setattr(fake_gs, "handle", logout_to_login)
    auth.get_connection()
    auth.reset_connection()
    assert logouts == [BASE + "/logout"]
    assert auth._connection is None
    assert getattr(auth._local, "expired", None) is None


def test_get_connection_logs_in_once_under_concurrent_first_calls(fake_gs) -> None:
    """mcp v2 runs sync tools on worker threads, so first calls can race.

    Without the lock every racing thread would log in (and the losers'
    sessions would be dropped); with it exactly one login happens.
    """
    fake_gs.login_delay = 0.05  # widen the race window

    start = threading.Barrier(8)
    results = []

    def worker() -> None:
        start.wait()
        results.append(auth.get_connection())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fake_gs.logins == 1
    assert len(results) == 8
    assert len({id(conn) for conn in results}) == 1
