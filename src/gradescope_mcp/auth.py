"""Authentication module for Gradescope.

Maintains a singleton GSConnection and exposes helpers that recover from
session expiry by re-authenticating once.

Earlier versions of this module advertised "automatic re-login" but never
implemented it: ``_connection.logged_in`` is only set to ``True`` once at
login and never reset, so the cached connection looked valid forever even
after Gradescope's server-side cookie had expired. The recovery path here
(``with_session_retry`` and ``request_with_retry``) provides that behavior
explicitly so callers can opt in.
"""

import logging
import os
from http.cookies import SimpleCookie
from typing import Callable, TypeVar

import requests
from gradescopeapi.classes.connection import GSConnection, Account

logger = logging.getLogger(__name__)

# Singleton connection instance
_connection: GSConnection | None = None

T = TypeVar("T")


class AuthError(Exception):
    """Raised when authentication fails."""
    pass


def get_connection() -> GSConnection:
    """Return the cached authenticated GSConnection, creating it if needed.

    Note: ``GSConnection.logged_in`` is set once at login and never reset by
    the upstream library, so this function cannot detect a server-side
    session expiry on its own. Use :func:`with_session_retry` or
    :func:`request_with_retry` when calling Gradescope endpoints to get
    automatic re-authentication.
    """
    global _connection

    if _connection is not None and _connection.logged_in:
        return _connection

    cookie_header = os.environ.get("GRADESCOPE_COOKIE_HEADER")
    session_cookie = os.environ.get("GRADESCOPE_SESSION_COOKIE")
    if cookie_header or session_cookie:
        _connection = _connection_from_cookies(cookie_header, session_cookie)
        logger.info("Loaded Gradescope session from cookie environment.")
        return _connection

    email = os.environ.get("GRADESCOPE_EMAIL")
    password = os.environ.get("GRADESCOPE_PASSWORD")

    if not email or not password:
        raise AuthError(
            "Missing Gradescope credentials. "
            "Set GRADESCOPE_COOKIE_HEADER for SSO sessions, or set "
            "GRADESCOPE_EMAIL and GRADESCOPE_PASSWORD for direct login."
        )

    try:
        conn = GSConnection()
        conn.login(email, password)
        _connection = conn
        logger.info("Logged in to Gradescope.")
        return _connection
    except ValueError as e:
        raise AuthError(f"Gradescope login failed: {e}") from e
    except Exception as e:
        # repr() preserves the exception type when str(e) is empty
        # (some requests exceptions stringify to "").
        raise AuthError(f"Unexpected error during login: {e!r}") from e


def _connection_from_cookies(
    cookie_header: str | None,
    session_cookie: str | None,
) -> GSConnection:
    """Build a Gradescope connection from browser-exported session cookies.

    This supports SSO-only accounts where the MCP cannot log in with
    ``GRADESCOPE_EMAIL`` / ``GRADESCOPE_PASSWORD``. ``GRADESCOPE_COOKIE_HEADER``
    should be a standard HTTP cookie header copied from an authenticated
    Gradescope browser session. ``GRADESCOPE_SESSION_COOKIE`` is a narrower
    fallback for only the Rails ``_gradescope_session`` cookie value.
    """
    conn = GSConnection()

    if cookie_header:
        parsed = SimpleCookie()
        try:
            parsed.load(cookie_header)
        except Exception as e:
            raise AuthError(f"Invalid GRADESCOPE_COOKIE_HEADER: {e}") from e
        if not parsed:
            raise AuthError("GRADESCOPE_COOKIE_HEADER did not contain cookies.")
        for morsel in parsed.values():
            conn.session.cookies.set(
                morsel.key,
                morsel.value,
                domain=".gradescope.com",
                path="/",
            )
    elif session_cookie:
        conn.session.cookies.set(
            "_gradescope_session",
            session_cookie,
            domain=".gradescope.com",
            path="/",
        )

    conn.logged_in = True
    conn.account = Account(conn.session, conn.gradescope_base_url)
    return conn


def reset_connection() -> None:
    """Drop the cached connection so the next ``get_connection()`` re-logs in.

    Invokes the upstream ``logout`` (added in gradescopeapi 1.8.0) on a
    best-effort basis to release the server-side session before discarding
    the local handle. Failures are logged and otherwise ignored — the
    primary contract is local state cleanup.
    """
    global _connection
    if _connection is not None:
        try:
            logout = getattr(_connection, "logout", None)
            if callable(logout):
                logout()
        except Exception as e:
            logger.warning("Best-effort logout failed during reset: %r", e)
    _connection = None


def is_session_expired_response(resp: requests.Response) -> bool:
    """Return True if ``resp`` indicates Gradescope rejected the session.

    Gradescope returns 401 for JSON endpoints and 302 → /login (or
    /account/auth) for HTML endpoints when the session has expired. Both
    indicate the cached connection is stale.
    """
    if resp.status_code == 401:
        return True
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "") or ""
        if "/login" in location or "/account/auth" in location:
            return True
    return False


def with_session_retry(call: Callable[[GSConnection], T]) -> T:
    """Run ``call(conn)`` with one automatic re-login on session expiry.

    The callable receives the live connection and must either:
    - return its result on success, or
    - raise an exception whose ``response`` attribute carries the failing
      ``requests.Response`` (the pattern used by ``raise_for_status``), or
    - return a ``requests.Response`` that triggers
      :func:`is_session_expired_response`.

    The wrapper inspects either signal, calls :func:`reset_connection`, and
    invokes ``call`` exactly one more time. Any second failure propagates
    untouched so the caller can surface it.
    """
    conn = get_connection()
    try:
        result = call(conn)
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        if resp is not None and is_session_expired_response(resp):
            reset_connection()
            return call(get_connection())
        raise

    # Duck-type the response check so callers can return either a real
    # ``requests.Response`` or any object with ``status_code`` + ``headers``.
    if hasattr(result, "status_code") and hasattr(result, "headers") \
            and is_session_expired_response(result):
        reset_connection()
        return call(get_connection())
    return result


def request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """Issue an HTTP request via the cached session with one re-login retry.

    Uses ``allow_redirects=False`` by default so we can spot the 302 → /login
    handshake; pass ``allow_redirects=True`` explicitly to opt back in for a
    given call.
    """
    kwargs.setdefault("allow_redirects", False)

    def _do(conn: GSConnection) -> requests.Response:
        return conn.session.request(method, url, **kwargs)

    return with_session_retry(_do)
