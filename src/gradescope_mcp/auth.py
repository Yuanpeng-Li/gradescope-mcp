"""Authentication, session recovery and HTTP timeouts for Gradescope.

``get_connection()`` returns one process-wide ``GSConnection`` (gradescopeapi's
container for ``.session``, ``.account`` and ``.gradescope_base_url``) that
every tool module shares. This module performs the login itself instead of
calling ``GSConnection.login`` so it controls how the credentials travel and
what an error may say:

- **Form-body login.** The credentials are POSTed to ``/login`` as a form
  body. gradescopeapi sends them as URL query parameters, which puts the
  password into access logs and into the text of connection errors.
- **No credentials in errors.** A failed login raises ``AuthError`` naming
  the exception class and a scrubbed reason; the password, the email and any
  ``session[...]=`` query fragments are redacted from messages and logs.
- **Failed-login cooldown.** Once Gradescope rejects the credentials, later
  calls raise the same ``AuthError`` without contacting Gradescope until
  ``GRADESCOPE_EMAIL`` / ``GRADESCOPE_PASSWORD`` change. Network failures and
  server errors are not cached.
- **Default timeouts.** The session's adapters apply a timeout (connect 10 s,
  read 60 s) to every request that does not pass its own, including the
  requests gradescopeapi helpers make on the same session.
  ``GRADESCOPE_MCP_HTTP_TIMEOUT=<seconds>`` sets the read timeout (and caps
  the connect timeout).
- **Session expiry.** Gradescope's server-side session eventually expires
  while the process still holds the cookie, and ``GSConnection.logged_in``
  never resets on its own. A response hook installed after login raises
  ``SessionExpiredError`` when Gradescope redirects to ``/login`` (or
  ``/account/auth``), serves its login page, or answers 401 "You must be
  logged in", and flags the current thread. ``with_session_recovery``
  (applied to every MCP tool and resource in ``server.py``) sees the flag,
  drops the expired connection and re-runs the call once on a fresh login,
  so a call logs in again at most once.

Since mcp v2, sync tool functions run on worker threads and can execute
concurrently, so creating and dropping the singleton is serialized with a
lock, and the "expired during this call" flag is thread-local. The shared
``requests.Session`` itself is used concurrently.
"""

import functools
import hashlib
import inspect
import logging
import math
import os
import re
import threading
from typing import Callable, TypeVar
from urllib.parse import quote, quote_plus, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from gradescopeapi.classes.account import Account
from gradescopeapi.classes.connection import GSConnection
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# Singleton connection instance
_connection: GSConnection | None = None
# Guards creation and reset of ``_connection`` (and ``_failed_login``) across
# tool worker threads.
_connection_lock = threading.Lock()
# Fingerprint of the credentials Gradescope last rejected (never the
# credentials themselves); cleared by the next successful login.
_failed_login: str | None = None
# Per-thread state: ``expired`` is the connection whose session expired during
# the current call (set by the response hook); ``recovering`` is True inside
# the outermost ``with_session_recovery`` wrapper.
_local = threading.local()

T = TypeVar("T")

HTTP_TIMEOUT_ENV = "GRADESCOPE_MCP_HTTP_TIMEOUT"
DEFAULT_TIMEOUT: tuple[float, float] = (10.0, 60.0)  # (connect, read) seconds

INVALID_CREDENTIALS_MESSAGE = "Gradescope login failed: invalid credentials."
_RECOVERY_FAILED = "Gradescope session expired and re-login did not restore access."
SESSION_RECOVERY_FAILED_MESSAGE = f"Authentication error: {_RECOVERY_FAILED}"

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_LOGIN_PATHS = ("/login", "/account/auth")
# ``session[password]=...`` / ``session%5Bemail%5D=...`` query fragments, as
# gradescopeapi's query-string login would put them into a URL.
_CREDENTIAL_QUERY_RE = re.compile(
    r"(session(?:\[|%5B|%255B)(?:password|email)(?:\]|%5D|%255D)=)[^&#\s'\")]*",
    re.IGNORECASE,
)
_MAX_REASON_CHARS = 300
_EXPIRED_UNKNOWN = object()


class AuthError(Exception):
    """Raised when authentication fails."""
    pass


class SessionExpiredError(AuthError):
    """Raised when Gradescope rejects the session cookie of a logged-in connection."""
    pass


class _InvalidCredentials(Exception):
    """Gradescope answered the login POST without its success redirect."""


# ------------------------------------------------------------------
# Timeouts
# ------------------------------------------------------------------


class TimeoutHTTPAdapter(HTTPAdapter):
    """``HTTPAdapter`` that applies a default timeout when the caller set none.

    requests waits forever by default, so one stalled socket would hang a
    tool call indefinitely. Everything else, including connection pooling and
    retries, is the stock ``HTTPAdapter`` that ``requests.Session`` mounts.
    """

    __attrs__ = HTTPAdapter.__attrs__ + ["default_timeout"]

    def __init__(self, *args, default_timeout=DEFAULT_TIMEOUT, **kwargs):
        self.default_timeout = default_timeout
        super().__init__(*args, **kwargs)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if timeout is None:
            timeout = self.default_timeout
        return super().send(
            request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies
        )


def _default_timeout() -> tuple[float, float]:
    """Return the (connect, read) timeout, honoring ``GRADESCOPE_MCP_HTTP_TIMEOUT``."""
    raw = os.environ.get(HTTP_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        logger.warning(
            "Ignoring invalid %s=%r; using the default timeout %s.",
            HTTP_TIMEOUT_ENV, raw, DEFAULT_TIMEOUT,
        )
        return DEFAULT_TIMEOUT
    return (min(DEFAULT_TIMEOUT[0], seconds), seconds)


def _new_connection() -> GSConnection:
    """Create a logged-out ``GSConnection`` whose session has default timeouts."""
    conn = GSConnection()
    timeout = _default_timeout()
    for prefix in ("https://", "http://"):
        conn.session.mount(prefix, TimeoutHTTPAdapter(default_timeout=timeout))
    return conn


# ------------------------------------------------------------------
# Credential scrubbing
# ------------------------------------------------------------------


def _scrub(text: str, *secrets: str) -> str:
    """Redact credentials from ``text``.

    Removes every literal, escaped or URL-encoded occurrence of the given
    secrets, then any remaining ``session[password]=`` / ``session[email]=``
    query values (raw or percent-encoded). Secrets go first so a raw password
    containing ``&`` is not cut at the ``&`` and partly left behind.
    """
    variants: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        variants.update({
            secret,
            quote(secret),
            quote(secret, safe=""),
            quote_plus(secret),
            secret.encode("unicode_escape").decode("ascii"),
            repr(secret)[1:-1],
        })
    variants.discard("")
    if variants:
        pattern = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
        text = re.sub(pattern, "[REDACTED]", text, flags=re.IGNORECASE)
    return _CREDENTIAL_QUERY_RE.sub(r"\1[REDACTED]", text)


def _describe_failure(exc: BaseException, *secrets: str) -> str:
    """Return ``"<ExceptionClass>: <scrubbed, truncated reason>"`` for ``exc``."""
    reason = " ".join(_scrub(str(exc), *secrets).split())
    if len(reason) > _MAX_REASON_CHARS:
        reason = reason[: _MAX_REASON_CHARS - 3] + "..."
    name = type(exc).__name__
    return f"{name}: {reason}" if reason else name


# ------------------------------------------------------------------
# Login
# ------------------------------------------------------------------


def _site(url: str) -> str:
    """Return the URL's host without a leading ``www.`` (for same-site checks)."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _is_login_path(path: str) -> bool:
    path = path.rstrip("/").lower()
    return any(path == p or path.startswith(p + "/") for p in _LOGIN_PATHS)


def _login(conn: GSConnection, email: str, password: str) -> None:
    """Log ``conn`` in, sending the credentials as a form body.

    Mirrors gradescopeapi's ``GSConnection.login``: GET the home page for the
    authenticity token and the initial session cookie, POST ``/login``, treat a
    302 as success, then copy the landing page's CSRF token into the session
    headers and attach an ``Account``.

    Raises:
        _InvalidCredentials: Gradescope answered without the success redirect.
        AuthError: an unexpected page (missing token, server error, or a
            redirect back to the login page).
        requests.RequestException: network failures; the caller scrubs them.
    """
    session = conn.session
    base_url = conn.gradescope_base_url.rstrip("/")

    home = session.get(base_url)
    token_input = BeautifulSoup(home.text, "html.parser").select_one(
        'form[action="/login"] input[name="authenticity_token"]'
    )
    if token_input is None or not token_input.get("value"):
        raise AuthError(
            "Gradescope login failed: the login form was not found on the "
            f"Gradescope home page (HTTP {home.status_code})."
        )

    login_data = {
        "utf8": "✓",
        "session[email]": email,
        "session[password]": password,
        "session[remember_me]": 0,
        "commit": "Log In",
        "session[remember_me_sso]": 0,
        "authenticity_token": token_input["value"],
    }
    resp = session.post(f"{base_url}/login", data=login_data)

    redirected = bool(resp.history) and resp.history[0].status_code == requests.codes.found
    if not redirected:
        if resp.status_code >= 500 or resp.status_code == 429:
            raise AuthError(
                f"Gradescope login failed: Gradescope answered HTTP {resp.status_code}; "
                "try again later."
            )
        raise _InvalidCredentials()
    if _is_login_path(urlsplit(resp.url).path):
        # Not cached as invalid credentials: upstream would even have counted
        # this as success, so its meaning is unknown.
        raise AuthError(
            "Gradescope login failed: Gradescope redirected back to the login page."
        )

    csrf = BeautifulSoup(resp.text, "html.parser").select_one('meta[name="csrf-token"]')
    if csrf is None or not csrf.get("content"):
        raise AuthError(
            "Gradescope login failed: no CSRF token on the page Gradescope "
            "showed after login."
        )

    session.cookies.update(resp.cookies)
    session.headers.update({"X-CSRF-Token": csrf["content"]})
    conn.logged_in = True
    conn.account = Account(session, conn.gradescope_base_url)


def _credential_fingerprint(email: str, password: str) -> str:
    return hashlib.sha256(f"{email}\0{password}".encode("utf-8")).hexdigest()


# ------------------------------------------------------------------
# Session-expiry detection
# ------------------------------------------------------------------


def _expiry_reason(response: requests.Response, site: str) -> str | None:
    """Return why ``response`` shows an expired session, or None if it doesn't."""
    if _site(response.url) != site:
        return None
    if response.status_code in _REDIRECT_CODES:
        target = urljoin(response.url, response.headers.get("Location") or "")
        if _site(target) == site and _is_login_path(urlsplit(target).path):
            return "redirected to the login page"
        return None
    if _is_login_path(urlsplit(response.url).path):
        return "Gradescope served its login page"
    if response.status_code == 401:
        try:
            body = response.text
        except Exception:
            body = ""
        # A 401 "You are not authorized to access this page." is a permission
        # error on a live session, not an expiry.
        if "must be logged in" in body.lower():
            return "HTTP 401: you must be logged in"
    return None


def _install_expiry_hook(conn: GSConnection) -> None:
    """Make ``conn.session`` raise ``SessionExpiredError`` on expiry signals.

    Installed only after a successful login on that connection's session, so
    the login flow itself (which legitimately visits ``/login``) never passes
    through it; a re-login always uses a fresh connection.

    The hook runs on every response, including each redirect hop, so a request
    answered with a redirect to the login page raises before requests follows
    it (and before it could turn a POST into a GET of the login page).
    """
    site = _site(conn.gradescope_base_url)

    def _check_session(response, *args, **kwargs):
        reason = _expiry_reason(response, site)
        if reason is None:
            return response
        _local.expired = conn
        try:
            response.content  # Read the (small) body so the socket can be reused.
            response.close()
        except Exception:
            pass
        raise SessionExpiredError(f"Gradescope session expired ({reason}).")

    _check_session._gradescope_expiry_hook = True
    conn.session.hooks["response"].append(_check_session)


def _remove_expiry_hook(conn: GSConnection) -> None:
    """Detach the hook installed by ``_install_expiry_hook``, if any."""
    session = getattr(conn, "session", None)
    hooks = getattr(session, "hooks", None)
    if not isinstance(hooks, dict):
        return
    hooks["response"] = [
        h for h in hooks.get("response", []) if not getattr(h, "_gradescope_expiry_hook", False)
    ]


# ------------------------------------------------------------------
# Connection singleton
# ------------------------------------------------------------------


def get_connection() -> GSConnection:
    """Return the cached authenticated GSConnection, logging in if needed.

    An expired session is not detected here: requests on it raise
    ``SessionExpiredError``, and ``with_session_recovery`` resets the
    connection so that its retry logs in again.

    Raises:
        AuthError: missing or rejected credentials (rejections are cached
            until the credentials change), or a network/server failure during
            login. Messages never contain the credentials.
    """
    global _connection, _failed_login

    conn = _connection
    if conn is not None and conn.logged_in:
        return conn

    with _connection_lock:
        # Another thread may have logged in while we waited for the lock.
        if _connection is not None and _connection.logged_in:
            return _connection

        email = os.environ.get("GRADESCOPE_EMAIL")
        password = os.environ.get("GRADESCOPE_PASSWORD")

        if not email or not password:
            raise AuthError(
                "Missing Gradescope credentials. "
                "Set GRADESCOPE_EMAIL and GRADESCOPE_PASSWORD environment variables."
            )

        fingerprint = _credential_fingerprint(email, password)
        if _failed_login == fingerprint:
            raise AuthError(INVALID_CREDENTIALS_MESSAGE)

        conn = _new_connection()
        try:
            _login(conn, email, password)
        except _InvalidCredentials:
            _failed_login = fingerprint
            logger.warning(
                "Gradescope rejected the configured credentials; not retrying "
                "until GRADESCOPE_EMAIL or GRADESCOPE_PASSWORD changes."
            )
            raise AuthError(INVALID_CREDENTIALS_MESSAGE) from None
        except AuthError as e:
            message = _scrub(str(e), email, password)
            logger.warning("%s", message)
            raise AuthError(message) from None
        except requests.RequestException as e:
            # ``from None``: the chained exception's text is not scrubbed.
            reason = _describe_failure(e, email, password)
            logger.warning("Gradescope login failed: %s", reason)
            raise AuthError(
                f"Gradescope login failed: network error while contacting Gradescope ({reason})."
            ) from None
        except Exception as e:
            reason = _describe_failure(e, email, password)
            logger.warning("Gradescope login failed: %s", reason)
            raise AuthError(f"Gradescope login failed: unexpected error ({reason}).") from None

        _install_expiry_hook(conn)
        _failed_login = None
        _connection = conn
        logger.info("Logged in to Gradescope.")
        return _connection


def reset_connection(expired: GSConnection | None = None) -> None:
    """Drop the cached connection so the next ``get_connection()`` re-logs in.

    Args:
        expired: The connection whose session Gradescope rejected, if that is
            why we reset. It is dropped only while it is still the cached one
            (another thread that saw the same expiry may already have logged
            in again; that fresh connection is kept), and it is not logged
            out, since Gradescope already ended its session.

    Without ``expired``, the upstream ``logout`` is invoked on a best-effort
    basis (with the expiry hook detached, so its redirect is not mistaken for
    an expiry) to release the server-side session. Failures are logged and
    otherwise ignored — the primary contract is local state cleanup.
    """
    global _connection
    with _connection_lock:
        conn = _connection
        if conn is None or (expired is not None and conn is not expired):
            return
        _connection = None
        if expired is not None:
            return
        _remove_expiry_hook(conn)
        try:
            logout = getattr(conn, "logout", None)
            if callable(logout):
                logout()
        except Exception as e:
            logger.warning("Best-effort logout failed during reset: %s", type(e).__name__)


# ------------------------------------------------------------------
# Session recovery
# ------------------------------------------------------------------


def _call_tracking_expiry(fn, args, kwargs):
    """Call ``fn``; return ``(expired, result, exception)``.

    ``expired`` is the connection whose session expired during the call (or
    ``_EXPIRED_UNKNOWN``), or None. Exceptions unrelated to an expiry
    propagate.
    """
    _local.expired = None
    try:
        result = fn(*args, **kwargs)
    except SessionExpiredError as exc:
        expired = getattr(_local, "expired", None)
        return (_EXPIRED_UNKNOWN if expired is None else expired), None, exc
    except Exception as exc:
        expired = getattr(_local, "expired", None)
        if expired is None:
            raise
        return expired, None, exc
    return getattr(_local, "expired", None), result, None


def with_session_recovery(fn: Callable[..., T]) -> Callable[..., T]:
    """Re-run ``fn`` once on a fresh login if Gradescope's session expired during it.

    Each call clears a thread-local flag, runs ``fn``, and checks whether the
    session's response hook set the flag. Tools usually catch the hook's
    ``SessionExpiredError`` and turn it into an error string (or a plausible
    empty result), so the flag, not the return value, is the signal. If it
    was set, the expired connection is dropped (``reset_connection``) and
    ``fn`` runs once more, logging in again on its first request. If the
    session expires again, a string-returning call gets
    ``SESSION_RECOVERY_FAILED_MESSAGE`` (``"Authentication error: Gradescope
    session expired and re-login did not restore access."``) instead of its
    misleading output; a call that raised gets ``SessionExpiredError`` with
    that text. Other exceptions and login failures pass through unchanged.

    Re-running the whole call is safe: the hook raises on the response to the
    rejected request itself (a redirect to the login page or a 401 "must be
    logged in"), before requests follows any redirect, so Gradescope did not
    process that request — a write that was redirected to ``/login`` did not
    happen. Later requests of the same call on that session are rejected the
    same way. Requests that succeeded (before the expiry, or on a connection
    another thread had already re-logged in) are repeated by the second run;
    in this server those are reads, or state-setting writes (a grade's rubric
    items, adjustment and comment) that produce the same result when re-sent.
    The one unguarded case is a write whose own response redirects to another
    page and the session expires before that hop; the window is a single
    redirect and is accepted.

    The hook does not mark the connection logged out, so a tool that keeps
    going after an expiry (e.g. a batch that records per-row errors) does
    not log in again per row: each call logs in again at most once.

    Works with sync functions only (mcp v2 runs them on worker threads; the
    flag is per thread). The wrapper keeps ``fn``'s name, docstring and
    signature (``functools.wraps``), so MCP builds the same schema. Nested
    wrapped calls defer to the outermost wrapper, so a call is retried at
    most once.
    """
    if inspect.iscoroutinefunction(fn):
        raise TypeError("with_session_recovery supports sync functions only.")

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if getattr(_local, "recovering", False):
            return fn(*args, **kwargs)
        _local.recovering = True
        try:
            expired, result, exc = _call_tracking_expiry(fn, args, kwargs)
            if expired is None:
                return result

            logger.warning(
                "Gradescope session expired during %s; logging in again and retrying once.",
                fn.__name__,
            )
            reset_connection(None if expired is _EXPIRED_UNKNOWN else expired)

            expired, result, exc = _call_tracking_expiry(fn, args, kwargs)
            if expired is None:
                return result

            logger.warning("Gradescope session expired again during %s after re-login.", fn.__name__)
            if exc is None and isinstance(result, str):
                return SESSION_RECOVERY_FAILED_MESSAGE
            raise SessionExpiredError(_RECOVERY_FAILED) from exc
        finally:
            _local.recovering = False
            _local.expired = None

    return wrapper
