"""Loading ``.env`` files, at startup and again before a login.

Configuration comes from environment variables. At startup ``main()`` loads
``.env`` files with python-dotenv, never overriding a variable that is
already set. Precedence, highest first:

1. The process environment, e.g. the MCP client's ``env`` block.
2. ``.env`` in the current working directory.
3. ``.env`` of the gradescope-mcp source checkout the server runs from, i.e.
   when this package sits at ``<checkout>/src/gradescope_mcp`` and
   ``<checkout>/pyproject.toml`` declares the ``gradescope-mcp`` project.

No other directory is searched. In particular the parents of the working
directory or of an installed package (``~``, ``/tmp``, a virtualenv's
parents) are never consulted: a ``.env`` planted there could route the login
through a proxy (``HTTPS_PROXY``) or lift the upload restrictions. On POSIX,
a ``.env`` owned by another user (other than root) or writable by everyone is
skipped. The files loaded or skipped are logged at startup.

Problems finding or reading a ``.env`` never stop the server from starting:
a working directory that no longer exists, or a ``.env`` that cannot be
opened or decoded (e.g. a root-owned ``0600`` file), is skipped with a
warning instead.

Credentials are re-read later as well. When ``main()`` loaded the files with
``remember_credentials=True``, ``refresh_credentials()`` (called by
``auth.get_connection()`` before every login attempt) reads
``GRADESCOPE_EMAIL`` and ``GRADESCOPE_PASSWORD`` from the same files again,
so fixing a password in ``.env`` takes effect on the next tool call instead
of after a restart. Only the credential variables that came from ``.env`` at
startup are refreshed; ones set in the process environment (an MCP client's
``env`` block) still win and need a restart to change.
"""

import io
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

CREDENTIAL_KEYS = ("GRADESCOPE_EMAIL", "GRADESCOPE_PASSWORD")


@dataclass(frozen=True)
class _RefreshState:
    cwd: Path | None
    package_dir: Path
    keys: tuple[str, ...]  # credential variables that came from .env


# Set by load_env_files(remember_credentials=True); None means "don't refresh".
_refresh: _RefreshState | None = None

_PROJECT_NAME_RE = re.compile(r"""^\s*name\s*=\s*["']gradescope[-_.]mcp["']\s*(?:#.*)?$""")


def _declares_gradescope_mcp(pyproject: Path) -> bool:
    """Whether ``pyproject`` has ``name = "gradescope-mcp"`` in its ``[project]`` table."""
    try:
        text = pyproject.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    table = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            table = stripped
            continue
        if table == "[project]" and _PROJECT_NAME_RE.match(line):
            return True
    return False


def source_checkout(package_dir: Path) -> Path | None:
    """The gradescope-mcp source checkout ``package_dir`` belongs to, if any.

    Only the src layout of this repository counts
    (``<checkout>/src/gradescope_mcp`` with ``<checkout>/pyproject.toml``);
    no other ancestor is looked at.
    """
    if package_dir.parent.name != "src":
        return None
    checkout = package_dir.parent.parent
    return checkout if _declares_gradescope_mcp(checkout / "pyproject.toml") else None


def dotenv_candidates(cwd: Path | None, package_dir: Path) -> list[Path]:
    """Existing ``.env`` files to load, highest precedence first.

    ``cwd`` is None when the working directory is unavailable. A file whose
    existence cannot be checked (e.g. permission denied) is still listed, so
    the loader reports why it was skipped instead of dropping it silently.
    """
    candidates = [] if cwd is None else [cwd / ".env"]
    checkout = source_checkout(package_dir)
    if checkout is not None:
        candidates.append(checkout / ".env")
    files: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            pass
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):  # RuntimeError: symlink loop
            resolved = path
        if resolved not in seen:
            seen.add(resolved)
            files.append(path)
    return files


def _os_reason(e: OSError) -> str:
    """A short description of ``e`` for a log line."""
    return e.strerror or type(e).__name__


def _untrusted_reason(path: Path) -> str | None:
    """Why ``path`` must not be loaded, or None if it may be."""
    try:
        info = path.stat()
    except OSError as e:
        return f"cannot be read ({_os_reason(e)})"
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # Windows: no POSIX ownership to check
        return None
    if info.st_uid not in (getuid(), 0):
        return "it is owned by another user"
    if info.st_mode & stat.S_IWOTH:
        return "it is writable by every user"
    return None


def load_env_files(
    cwd: Path | None = None,
    package_dir: Path | None = None,
    *,
    remember_credentials: bool = False,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Load the ``.env`` files described in the module docstring.

    Returns the files loaded and the files skipped (with the reason), in
    precedence order. Variables already in the environment are kept.

    With ``remember_credentials`` (``main()`` passes it), the credential
    variables not already in the environment are remembered so that
    ``refresh_credentials()`` can re-read them from the same files later.
    """
    global _refresh
    external = {key for key in CREDENTIAL_KEYS if key in os.environ}
    loaded: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    if cwd is None:
        try:
            cwd = Path.cwd()
        except OSError as e:
            # E.g. the client was started in a directory that has since been
            # removed. Skip the working directory's .env rather than fail.
            skipped.append(
                (Path(".env"), f"the working directory is unavailable ({_os_reason(e)})")
            )
    if package_dir is None:
        try:
            package_dir = Path(__file__).resolve().parent
        except (OSError, RuntimeError):
            package_dir = Path(__file__).parent
    for path in dotenv_candidates(cwd, package_dir):
        reason = _untrusted_reason(path)
        if reason is None:
            # Read the file here so an unreadable or undecodable file (a
            # root-owned 0600 .env passes the ownership check) is skipped
            # with a reason instead of raising out of main(). Parsing the
            # text in full before setting anything keeps the load atomic.
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                reason = f"cannot be read ({_os_reason(e)})"
            except UnicodeDecodeError:
                reason = "it is not valid UTF-8 text"
        if reason is not None:
            skipped.append((path, reason))
            continue
        load_dotenv(stream=io.StringIO(text), override=False)
        loaded.append(path)
    if remember_credentials:
        managed = tuple(key for key in CREDENTIAL_KEYS if key not in external)
        _refresh = _RefreshState(cwd, package_dir, managed) if managed else None
    return loaded, skipped


def refresh_credentials() -> list[str]:
    """Re-read the credential variables that came from ``.env`` at startup.

    Reads the same candidate files with the same trust checks and sets each
    managed variable to the value from the highest-precedence file that
    defines it (removing it when no file does any more). Variables that were
    already in the process environment at startup are never touched. A
    no-op unless ``load_env_files(remember_credentials=True)`` ran.

    Returns the names of the variables that changed (never their values).
    """
    state = _refresh
    if state is None:
        return []
    values: dict[str, str] = {}
    for path in dotenv_candidates(state.cwd, state.package_dir):
        if _untrusted_reason(path) is not None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parsed = dotenv_values(stream=io.StringIO(text))
        for key in state.keys:
            value = parsed.get(key)
            if key not in values and value:
                values[key] = value
    changed: list[str] = []
    for key in state.keys:
        new = values.get(key)
        if os.environ.get(key) == new:
            continue
        if new is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = new
        changed.append(key)
    return changed
