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
- **Failed-login cooldown.** When Gradescope answers a login attempt without
  logging in, later calls with the same credentials raise the same
  ``AuthError`` without contacting Gradescope until a cooldown ends (or
  ``GRADESCOPE_EMAIL`` / ``GRADESCOPE_PASSWORD`` change). Before every login
  attempt the credentials are re-read from the ``.env`` files loaded at
  startup (``envfiles.refresh_credentials``), so a password fixed in ``.env``
  is used on the next call without a restart. Only a re-rendered
  login form or an "invalid email/password" message counts as invalid
  credentials (10 minutes). HTTP 429 and 5xx answers wait for Gradescope's
  ``Retry-After`` (capped at 15 minutes; 1 minute without one); a "too many
  attempts" page waits 5 minutes; any other rejection (a 403 challenge, a 422
  CSRF failure, a redirect back to the login page) is reported as "login
  rejected (HTTP <status>)" and waits 1 minute. Network failures are not
  cached.
- **Default timeouts.** The session's adapters apply a timeout (connect 10 s,
  read 60 s) to every request that does not pass its own, including the
  requests gradescopeapi helpers make on the same session.
  ``GRADESCOPE_MCP_HTTP_TIMEOUT=<seconds>`` sets the read timeout (and caps
  the connect timeout).
- **Session expiry.** Gradescope's server-side session eventually expires
  while the process still holds the cookie, and ``GSConnection.logged_in``
  never resets on its own. A response hook installed after login raises
  ``SessionExpiredError`` when Gradescope redirects to ``/login`` (or
  ``/account/auth``), serves its login page, answers 401 "You must be logged
  in", or serves the logged-out home page (a page with the login form and no
  logout link, e.g. after a redirect to ``/``), and flags the current thread.
  A streamed body is never read by the hook: its logged-out-page check runs
  when the reader has read the body within its own limits
  (``check_streamed_body``).
  ``with_session_recovery`` (applied to every MCP tool and resource in
  ``server.py``) sees the flag and drops the expired connection. It re-runs
  the call once on a fresh login, so a call logs in again at most once,
  unless Gradescope had already accepted a write during the call: then the
  first result is returned with a notice instead (see
  ``with_session_recovery``).

Since mcp v2, sync tool functions run on worker threads and can execute
concurrently, so creating and dropping the singleton is serialized with a
lock, and the per-call state ("expired during this call", "writes accepted
during this call") is thread-local. The shared ``requests.Session`` itself is
used concurrently.
"""

import functools
import hashlib
import inspect
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, NamedTuple, TypeVar
from urllib.parse import quote, quote_plus, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Comment
from gradescopeapi.classes.account import Account
from gradescopeapi.classes.connection import GSConnection
from requests.adapters import HTTPAdapter

from gradescope_mcp import envfiles

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LoginFailure:
    """A login Gradescope answered without logging in, and its cooldown."""

    fingerprint: str  # of the credentials used (never the credentials themselves)
    reason: str  # scrubbed AuthError text, without the cooldown sentence
    retry_at: float  # ``_clock()`` value from which logging in is tried again


# Singleton connection instance
_connection: GSConnection | None = None
# Guards creation and reset of ``_connection`` (and ``_failed_login``) across
# tool worker threads.
_connection_lock = threading.Lock()
# The last failed login and its cooldown; cleared by the next successful login.
_failed_login: _LoginFailure | None = None
# Per-thread state of the current call: ``expired`` is the connection whose
# session expired (set by the response hook); ``writes`` counts same-site
# write requests (methods other than GET/HEAD/OPTIONS/TRACE) that Gradescope
# answered with a 2xx, or with a redirect that did not end in an expiry
# signal; ``pending_write`` is True while such a redirect is unresolved;
# ``recovering`` is True inside the outermost ``with_session_recovery``.
_local = threading.local()
# Monotonic clock for login cooldowns (a seam for tests).
_clock = time.monotonic

T = TypeVar("T")

HTTP_TIMEOUT_ENV = "GRADESCOPE_MCP_HTTP_TIMEOUT"
DEFAULT_TIMEOUT: tuple[float, float] = (10.0, 60.0)  # (connect, read) seconds

INVALID_CREDENTIALS_MESSAGE = "Gradescope login failed: invalid credentials."
# Where credentials come from, and how a fix takes effect without a restart.
CREDENTIALS_HINT = (
    "Edits to GRADESCOPE_EMAIL / GRADESCOPE_PASSWORD in .env are picked up on "
    "the next call; values set in the MCP client's env block need a server "
    "restart."
)
_RECOVERY_FAILED = "Gradescope session expired and re-login did not restore access."
SESSION_RECOVERY_FAILED_MESSAGE = f"Authentication error: {_RECOVERY_FAILED}"

# Login cooldowns, in seconds.
INVALID_CREDENTIALS_COOLDOWN = 600.0
REJECTED_LOGIN_COOLDOWN = 60.0
THROTTLED_LOGIN_COOLDOWN = 60.0  # HTTP 429/5xx without a usable Retry-After
TOO_MANY_ATTEMPTS_COOLDOWN = 300.0
MAX_LOGIN_COOLDOWN = 900.0

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_LOGIN_PATHS = ("/login", "/account/auth")
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_LOGOUT_URL_RE = re.compile(r"/logout(?:[/?#]|$)", re.IGNORECASE)
# Cheap pre-check before a page is parsed for the login form: an opening
# ``<form`` tag whose action is ``/login`` (relative or absolute).
_LOGIN_FORM_TAG_RE = re.compile(
    rb"<form\b[^>]*\baction\s*=\s*[\"']?(?:https?://[^\"'\s>/]+)?/login(?=[/?#\"'\s>])",
    re.IGNORECASE,
)
# Page text of a login refused because of the credentials.
_INVALID_CREDENTIALS_RE = re.compile(
    r"\b(?:invalid|incorrect|wrong)\b[^.!<]{0,40}\b(?:e-?mail|password|credentials)\b"
    r"|\b(?:e-?mail|password|credentials)\b[^.!<]{0,40}"
    r"\b(?:invalid|incorrect|wrong|not recognized|(?:did|do)(?: not|n't) match)\b",
    re.IGNORECASE,
)
# Page text of a login refused because of throttling or a lockout.
_TOO_MANY_ATTEMPTS_RE = re.compile(
    r"\btoo many\b[^.!<]{0,30}\b(?:attempts|requests|tries|log ?ins|sign[- ]?ins)\b"
    r"|\brate.?limit"
    r"|\b(?:account|login)\b[^.!<]{0,30}\b(?:locked|blocked)\b"
    r"|\btemporarily (?:locked|blocked)\b",
    re.IGNORECASE,
)
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


class _LoginRejected(Exception):
    """Gradescope answered a login attempt without logging in.

    ``str(exc)`` is the ``AuthError`` text (it never contains the
    credentials); ``cooldown`` is how many seconds to wait before trying again.
    """

    def __init__(self, reason: str, cooldown: float) -> None:
        super().__init__(reason)
        self.cooldown = cooldown


class _InvalidCredentials(_LoginRejected):
    """Gradescope re-showed the login form or said the credentials are wrong."""

    def __init__(self) -> None:
        super().__init__(
            f"{INVALID_CREDENTIALS_MESSAGE} Check GRADESCOPE_EMAIL and "
            f"GRADESCOPE_PASSWORD. {CREDENTIALS_HINT}",
            INVALID_CREDENTIALS_COOLDOWN,
        )


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


def _has_login_form(soup: BeautifulSoup, page_url: str, *, require_password: bool = False) -> bool:
    """Whether the page has a same-site form that posts to the login page.

    The form must carry the password field or (unless ``require_password``)
    the ``authenticity_token`` that ``_login`` scrapes from the home page.
    """
    site = _site(page_url)
    for form in soup.find_all("form"):
        action = urljoin(page_url, str(form.get("action") or ""))
        if _site(action) != site or not _is_login_path(urlsplit(action).path):
            continue
        if form.find("input", attrs={"name": "session[password]"}) is not None:
            return True
        if not require_password and form.find("input", attrs={"name": "authenticity_token"}):
            return True
    return False


def _is_logged_out_page(soup: BeautifulSoup, page_url: str) -> bool:
    """A page with the login form and no logout link (a logged-in page has one)."""
    if soup.find("a", href=_LOGOUT_URL_RE) or soup.find("form", action=_LOGOUT_URL_RE):
        return False
    return _has_login_form(soup, page_url)


def _visible_text(soup: BeautifulSoup) -> str:
    """The page's text outside scripts, styles and comments."""
    parts = [
        str(s) for s in soup.find_all(string=True)
        if not isinstance(s, Comment)
        and getattr(s.parent, "name", None) not in ("script", "style", "noscript", "template")
    ]
    return " ".join(" ".join(parts).split())


def _retry_after(response: requests.Response) -> float | None:
    """Seconds requested by a ``Retry-After`` header (seconds or HTTP date), if any."""
    raw = str(response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


def _cooldown(seconds: float) -> float:
    return min(MAX_LOGIN_COOLDOWN, max(1.0, seconds))


def _throttled(response: requests.Response, reason: str) -> _LoginRejected:
    """A rejection for HTTP 429/5xx, waiting as long as ``Retry-After`` asks."""
    retry_after = _retry_after(response)
    return _LoginRejected(
        reason, _cooldown(THROTTLED_LOGIN_COOLDOWN if retry_after is None else retry_after)
    )


def _login_failure(resp: requests.Response, soup: BeautifulSoup) -> _LoginRejected:
    """Classify a login POST answer that did not log in.

    Only a re-rendered login form (HTTP 200 or 422, no redirect) or a page
    saying the email/password is wrong counts as invalid credentials. A WAF
    challenge, a CSRF failure or an unexpected page is reported as "login
    rejected (HTTP <status>)", so a transient answer is not mistaken for bad
    credentials.
    """
    status = resp.status_code
    if status == 429 or status >= 500:
        return _throttled(
            resp, f"Gradescope login failed: Gradescope answered HTTP {status}; try again later."
        )
    text = _visible_text(soup)
    if _TOO_MANY_ATTEMPTS_RE.search(text):
        retry_after = _retry_after(resp)
        return _LoginRejected(
            "Gradescope login failed: Gradescope reports too many login attempts; "
            "try again later.",
            _cooldown(TOO_MANY_ATTEMPTS_COOLDOWN if retry_after is None else retry_after),
        )
    if _INVALID_CREDENTIALS_RE.search(text) or (
        not resp.history
        and status in (200, 422)
        and _has_login_form(soup, resp.url, require_password=True)
    ):
        return _InvalidCredentials()
    if resp.history and _is_login_path(urlsplit(resp.url).path):
        reason = "Gradescope redirected back to the login page"
    elif resp.history and _is_logged_out_page(soup, resp.url):
        reason = "Gradescope showed its login form again after the redirect"
    elif resp.history:
        reason = f"login rejected (HTTP {resp.history[0].status_code} redirect)"
    else:
        reason = f"login rejected (HTTP {status})"
    return _LoginRejected(f"Gradescope login failed: {reason}.", REJECTED_LOGIN_COOLDOWN)


def _login(conn: GSConnection, email: str, password: str) -> None:
    """Log ``conn`` in, sending the credentials as a form body.

    Mirrors gradescopeapi's ``GSConnection.login``: GET the home page for the
    authenticity token and the initial session cookie, POST ``/login``, treat a
    302 to a page without the login form as success, then copy the landing
    page's CSRF token into the session headers and attach an ``Account``.

    Raises:
        _LoginRejected: Gradescope answered without logging in
            (``_InvalidCredentials`` when it says the credentials are wrong),
            or the home page answered HTTP 429/5xx.
        AuthError: the home page has no login form.
        requests.RequestException: network failures; the caller scrubs them.
    """
    session = conn.session
    base_url = conn.gradescope_base_url.rstrip("/")

    home = session.get(base_url)
    token_input = BeautifulSoup(home.text, "html.parser").select_one(
        'form[action="/login"] input[name="authenticity_token"]'
    )
    if token_input is None or not token_input.get("value"):
        reason = (
            "Gradescope login failed: the login form was not found on the "
            f"Gradescope home page (HTTP {home.status_code})."
        )
        if home.status_code == 429 or home.status_code >= 500:
            raise _throttled(home, reason)
        raise AuthError(reason)

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

    soup = BeautifulSoup(resp.text, "html.parser")
    logged_in = (
        bool(resp.history)
        and resp.history[0].status_code == requests.codes.found
        and not _is_login_path(urlsplit(resp.url).path)
        and not _is_logged_out_page(soup, resp.url)
    )
    if not logged_in:
        raise _login_failure(resp, soup)

    csrf = soup.select_one('meta[name="csrf-token"]')
    if csrf is None or not csrf.get("content"):
        raise _LoginRejected(
            "Gradescope login failed: no CSRF token on the page Gradescope "
            "showed after login.",
            REJECTED_LOGIN_COOLDOWN,
        )

    session.cookies.update(resp.cookies)
    session.headers.update({"X-CSRF-Token": csrf["content"]})
    conn.logged_in = True
    conn.account = Account(session, conn.gradescope_base_url)


def _credential_fingerprint(email: str, password: str) -> str:
    return hashlib.sha256(f"{email}\0{password}".encode("utf-8")).hexdigest()


def _format_wait(seconds: float) -> str:
    seconds = max(1, math.ceil(seconds))
    minutes, secs = divmod(seconds, 60)
    if not minutes:
        return f"{secs} s"
    return f"{minutes} min" + (f" {secs} s" if secs else "")


def _with_cooldown(reason: str, seconds: float) -> str:
    return f"{reason} Not trying to log in again for {_format_wait(seconds)}."


# ------------------------------------------------------------------
# Session-expiry detection
# ------------------------------------------------------------------


def _content_type(response: requests.Response) -> str:
    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
    return content_type.strip().lower()


def _shows_logged_out_page(response: requests.Response, stream: bool = False) -> bool:
    """Whether a page is Gradescope's logged-out page: the login form, no logout link.

    Gradescope may send an expired session to its home page instead of
    ``/login``; requests follows that redirect (turning a POST into a GET),
    and the caller would get a 200 with the anonymous home page. Only HTML is
    inspected (a missing Content-Type counts as HTML). A streamed body is not
    read here (False): reading it would bypass the reader's size and time
    limits, so the hook leaves that check to ``check_streamed_body``.
    """
    content_type = _content_type(response)
    if stream or (content_type and content_type not in _HTML_TYPES):
        return False
    try:
        content = response.content
    except Exception:
        return False
    return _is_logged_out_body(response, content)


def _is_logged_out_body(response: requests.Response, content: bytes) -> bool:
    """Whether ``content``, the body of ``response``, is the logged-out page.

    It is parsed only if it has a ``<form`` tag posting to ``/login``. A page
    that also links to ``/logout`` belongs to a logged-in session and does
    not count.
    """
    if not content or not _LOGIN_FORM_TAG_RE.search(content):
        return False
    try:
        text = content.decode(response.encoding or "utf-8", errors="replace")
    except LookupError:
        text = content.decode("utf-8", errors="replace")
    return _is_logged_out_page(BeautifulSoup(text, "html.parser"), response.url)


def _expiry_reason(response: requests.Response, site: str, stream: bool = False) -> str | None:
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
    if 200 <= response.status_code < 300 and _shows_logged_out_page(response, stream):
        return "Gradescope served its logged-out page with the login form"
    return None


def _note_write(response: requests.Response, site: str) -> None:
    """Count a write Gradescope accepted during the current call (see ``_local``).

    A same-site write answered with a 2xx counts. One answered with a
    redirect stays pending until the next response on this thread (normally
    the redirect target): an expiry signal there means the session had
    already expired, so the write was not processed; anything else (or the
    end of the call) counts it.
    """
    method = str(getattr(response.request, "method", None) or "GET").upper()
    is_write = method not in _SAFE_METHODS and _site(response.url) == site
    if response.status_code in _REDIRECT_CODES:
        if is_write:
            _local.pending_write = True
        return
    if getattr(_local, "pending_write", False) or (
        is_write and 200 <= response.status_code < 300
    ):
        _local.writes = getattr(_local, "writes", 0) + 1
    _local.pending_write = False


def _install_expiry_hook(conn: GSConnection) -> None:
    """Make ``conn.session`` raise ``SessionExpiredError`` on expiry signals.

    Installed only after a successful login on that connection's session, so
    the login flow itself (which legitimately visits ``/login``) never passes
    through it; a re-login always uses a fresh connection.

    The hook runs on every response, including each redirect hop, so a request
    answered with a redirect to the login page raises before requests follows
    it (and before it could turn a POST into a GET of the login page). A
    redirect to another page that turns out to be the logged-out home page
    raises on that page. Responses that are not expiry signals are counted
    as accepted writes where they apply (``_note_write``). A streamed body is
    never read here (that would bypass the reader's size and time limits):
    for a streamed same-site HTML page the logged-out-page check is left to
    the reader (``check_streamed_body``).
    """
    site = _site(conn.gradescope_base_url)

    def _check_session(response, *args, **kwargs):
        stream = bool(kwargs.get("stream"))
        reason = _expiry_reason(response, site, stream=stream)
        if reason is None:
            _note_write(response, site)
            if stream:
                _defer_body_check(response, site, conn)
            return response
        _local.pending_write = False
        _local.expired = conn
        try:
            response.content  # Read the (small) body so the socket can be reused.
            response.close()
        except Exception:
            pass
        raise SessionExpiredError(f"Gradescope session expired ({reason}).")

    _check_session._gradescope_expiry_hook = True
    conn.session.hooks["response"].append(_check_session)


def _defer_body_check(response: requests.Response, site: str, conn: GSConnection) -> None:
    """Leave the logged-out-page check of a streamed response to its reader.

    Applies to a same-site 2xx HTML response (the kind ``_shows_logged_out_page``
    reads when not streamed); ``check_streamed_body`` runs the check.
    """
    if (
        _site(response.url) != site
        or not 200 <= response.status_code < 300
        or _content_type(response) not in _HTML_TYPES
    ):
        return

    def check(body: bytes) -> None:
        if _is_logged_out_body(response, body):
            _local.pending_write = False
            _local.expired = conn
            raise SessionExpiredError(
                "Gradescope session expired (Gradescope served its logged-out "
                "page with the login form)."
            )

    response._gradescope_body_check = check


def check_streamed_body(response: requests.Response, body: bytes) -> None:
    """Finish the expiry check of a streamed response once its body was read.

    The expiry hook does not read a streamed body, so the code that reads it
    (within its own size and time limits) passes the body here. Raises
    ``SessionExpiredError`` and flags the thread, as the hook does, when the
    body is Gradescope's logged-out page; does nothing for responses the
    hook did not defer.
    """
    check = getattr(response, "_gradescope_body_check", None)
    if check is not None:
        check(body)


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
        AuthError: missing or rejected credentials, or a network/server
            failure during login. A login Gradescope answered without logging
            in starts a cooldown (see the module docstring): until it ends,
            calls with the same credentials get the same error without
            contacting Gradescope. Messages never contain the credentials.
    """
    global _connection, _failed_login

    conn = _connection
    if conn is not None and conn.logged_in:
        return conn

    with _connection_lock:
        # Another thread may have logged in while we waited for the lock.
        if _connection is not None and _connection.logged_in:
            return _connection

        # Pick up credentials fixed in .env since startup (issue #8). Changed
        # credentials have a new fingerprint, so a cooldown from the old
        # ones no longer applies.
        changed = envfiles.refresh_credentials()
        if changed:
            logger.info("Re-read %s from .env.", " and ".join(changed))

        email = os.environ.get("GRADESCOPE_EMAIL")
        password = os.environ.get("GRADESCOPE_PASSWORD")

        if not email or not password:
            raise AuthError(
                "Missing Gradescope credentials. Set GRADESCOPE_EMAIL and "
                f"GRADESCOPE_PASSWORD (in .env or the MCP client's env block). "
                f"{CREDENTIALS_HINT}"
            )

        fingerprint = _credential_fingerprint(email, password)
        failure = _failed_login
        if failure is not None and failure.fingerprint == fingerprint:
            remaining = failure.retry_at - _clock()
            if remaining > 0:
                raise AuthError(_with_cooldown(failure.reason, remaining))

        conn = _new_connection()
        try:
            _login(conn, email, password)
        except _LoginRejected as e:
            reason = _scrub(str(e), email, password)
            _failed_login = _LoginFailure(fingerprint, reason, _clock() + e.cooldown)
            message = _with_cooldown(reason, e.cooldown)
            logger.warning("%s", message)
            raise AuthError(message) from None
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


class _Attempt(NamedTuple):
    """One run of a wrapped call."""

    expired: Any  # the connection whose session expired, _EXPIRED_UNKNOWN, or None
    result: Any
    exc: BaseException | None
    writes: int  # writes Gradescope accepted during the run (see ``_local``)


def _writes_this_call() -> int:
    """Accepted writes of the current call; an unresolved write redirect counts."""
    return getattr(_local, "writes", 0) + (1 if getattr(_local, "pending_write", False) else 0)


def _reset_call_state() -> None:
    _local.expired = None
    _local.writes = 0
    _local.pending_write = False


def _call_tracking_expiry(fn, args, kwargs) -> _Attempt:
    """Call ``fn`` and record whether the session expired and what it wrote.

    Exceptions unrelated to an expiry propagate.
    """
    _reset_call_state()
    try:
        result = fn(*args, **kwargs)
    except SessionExpiredError as exc:
        expired = getattr(_local, "expired", None)
        return _Attempt(
            _EXPIRED_UNKNOWN if expired is None else expired, None, exc, _writes_this_call()
        )
    except Exception as exc:
        expired = getattr(_local, "expired", None)
        if expired is None:
            raise
        return _Attempt(expired, None, exc, _writes_this_call())
    return _Attempt(getattr(_local, "expired", None), result, None, _writes_this_call())


def _writes_then_expiry_notice(writes: int) -> str:
    return (
        "⚠️ The Gradescope session expired during this call after Gradescope had "
        f"accepted {writes} write request(s), so the call was not re-run "
        "automatically (re-running could repeat those writes or report them as "
        "not done). Anything above that failed or could not be read back "
        "because of the expiry is unconfirmed: check the current state with "
        "the read tools before retrying it. The next call logs in again."
    )


def _report_writes_then_expiry(attempt: _Attempt) -> Any:
    """Return a run that wrote before the session expired, with a notice."""
    if attempt.exc is not None:
        raise SessionExpiredError(
            "Gradescope session expired during the call after Gradescope had "
            f"accepted {attempt.writes} write request(s); the call was not re-run. "
            "Check the current state with the read tools before retrying it."
        ) from attempt.exc
    if isinstance(attempt.result, str):
        return f"{attempt.result.rstrip()}\n\n{_writes_then_expiry_notice(attempt.writes)}"
    return attempt.result


def _report_failed_recovery(first: _Attempt, retry: _Attempt) -> str:
    """The recovery-failed error, followed by the output a run did produce."""
    for attempt, label in ((first, "the first attempt"), (retry, "the retry")):
        if attempt.exc is None and isinstance(attempt.result, str):
            return (
                f"{SESSION_RECOVERY_FAILED_MESSAGE}\n\n"
                f"Output of {label} (the session had expired, so it may be "
                f"incomplete or wrong):\n{attempt.result}"
            )
    raise SessionExpiredError(_RECOVERY_FAILED) from (retry.exc or first.exc)


def with_session_recovery(fn: Callable[..., T]) -> Callable[..., T]:
    """Re-run ``fn`` once on a fresh login if Gradescope's session expired during it.

    Each call clears the thread-local call state, runs ``fn``, and checks
    whether the session's response hook flagged an expiry. Tools usually
    catch the hook's ``SessionExpiredError`` and turn it into an error string
    (or a plausible empty result), so the flag, not the return value, is the
    signal. If it was set, the expired connection is dropped
    (``reset_connection``), and then:

    - **No write was accepted** during the call: ``fn`` runs once more,
      logging in again on its first request. This is safe because the hook
      raises on the response to the rejected request itself, before requests
      follows a redirect to the login page, so Gradescope did not process
      that request (a write redirected to ``/login`` or to the logged-out
      home page did not happen), and the requests that did succeed were reads.
    - **Gradescope had accepted a write** (a same-site non-GET request
      answered with a 2xx, or with a redirect that did not end in an expiry
      signal): ``fn`` is not re-run. A re-run could repeat the write, or
      contradict it: a re-run of a delete finds the item gone, and a re-run
      of a group grade finds the members graded, and both would then report
      that nothing was changed. The first result is returned with a notice
      that the session expired after N accepted write(s) and that anything
      the call could not confirm (e.g. a read-back) must be checked with the
      read tools. A call that raised gets ``SessionExpiredError`` saying so.

    If the session expires again during the re-run, a re-run that had a
    write accepted is reported the same way: its output plus the notice
    (not an error result), or ``SessionExpiredError`` if it raised.
    Otherwise a string-returning call gets ``SESSION_RECOVERY_FAILED_MESSAGE``
    (``"Authentication error: Gradescope session expired and re-login did
    not restore access."``) followed by the first attempt's output, or the
    re-run's if the first attempt raised, labelled as possibly incomplete
    (that output can be a misleading empty result, so it never stands on its
    own); if neither run returned text, ``SessionExpiredError`` with that
    text is raised.
    Other exceptions and login failures pass through unchanged.

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
            first = _call_tracking_expiry(fn, args, kwargs)
            if first.expired is None:
                return first.result
            reset_connection(None if first.expired is _EXPIRED_UNKNOWN else first.expired)
            if first.writes:
                logger.warning(
                    "Gradescope session expired during %s after %d accepted write(s); "
                    "not re-running it.",
                    fn.__name__, first.writes,
                )
                return _report_writes_then_expiry(first)

            logger.warning(
                "Gradescope session expired during %s; logging in again and retrying once.",
                fn.__name__,
            )
            retry = _call_tracking_expiry(fn, args, kwargs)
            if retry.expired is None:
                return retry.result

            logger.warning("Gradescope session expired again during %s after re-login.", fn.__name__)
            if retry.expired is not _EXPIRED_UNKNOWN:
                reset_connection(retry.expired)
            if retry.writes:
                return _report_writes_then_expiry(retry)
            return _report_failed_recovery(first, retry)
        finally:
            _local.recovering = False
            _reset_call_state()

    return wrapper
