"""Private per-user runtime cache for ephemeral MCP and skill artifacts.

The cache holds scanned student pages, grading artifacts and answer keys, and
agents read those files back as grading input. It must therefore be neither
readable nor writable by other local users:

- Root: ``$GRADESCOPE_MCP_CACHE_DIR`` when set. Otherwise
  ``$XDG_RUNTIME_DIR/gradescope-mcp`` when that runtime directory exists and
  is owned by the current user, else ``<tempdir>/gradescope-mcp-<uid>``.
- Directories are created with mode 0700. On every use the root is checked
  with ``lstat``: it must be a real directory (not a symlink), owned by the
  current user and closed to group/other. Otherwise :class:`CacheError` is
  raised instead of adopting a directory someone else prepared.
- Artifact names are limited to ``[A-Za-z0-9._-]`` and must resolve to a
  direct child of the root.
- Files are written with mode 0600 through a fresh ``O_EXCL | O_NOFOLLOW``
  temp file followed by an atomic ``os.replace``, so a planted symlink is
  never followed.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import re
import secrets
import stat
import tempfile

_CACHE_ROOT_ENV = "GRADESCOPE_MCP_CACHE_DIR"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_ARTIFACT_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_FIX_HINT = (
    f"Set {_CACHE_ROOT_ENV} to a directory owned by you with mode 0700 "
    "(or remove the offending path)."
)

# Owner/mode checks need POSIX uids; on platforms without them (Windows) the
# checks degrade to what the OS offers.
_HAS_UIDS = hasattr(os, "getuid")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


class CacheError(RuntimeError):
    """Raised when the cache location is unsafe or an artifact name is invalid."""


def _user_suffix() -> str:
    """Return a per-user suffix for the fallback root name."""
    if _HAS_UIDS:
        return str(os.getuid())
    try:
        import getpass

        user = getpass.getuser()
    except Exception:
        user = "user"
    return re.sub(r"[^A-Za-z0-9_-]", "_", user) or "user"


def default_cache_root() -> pathlib.Path:
    """Return the per-user default cache root (not created)."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir and _HAS_UIDS and os.path.isabs(runtime_dir):
        try:
            st = os.lstat(runtime_dir)
        except OSError:
            st = None
        if st is not None and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            return pathlib.Path(runtime_dir) / "gradescope-mcp"
    return pathlib.Path(tempfile.gettempdir()) / f"gradescope-mcp-{_user_suffix()}"


def _verify_private_dir(path: pathlib.Path, *, repair_mode: bool = False) -> None:
    """Raise CacheError unless ``path`` is a real directory private to this user.

    ``repair_mode`` tightens group/other bits instead of refusing; it is only
    used for directories inside an already-verified private root, where no
    other user can have planted anything.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise CacheError(f"Cache directory `{path}` does not exist.") from None
    if stat.S_ISLNK(st.st_mode):
        raise CacheError(
            f"Refusing to use cache directory `{path}`: it is a symlink. {_FIX_HINT}"
        )
    if not stat.S_ISDIR(st.st_mode):
        raise CacheError(
            f"Refusing to use cache directory `{path}`: it is not a directory. {_FIX_HINT}"
        )
    if not _HAS_UIDS:
        return
    if st.st_uid != os.getuid():
        raise CacheError(
            f"Refusing to use cache directory `{path}`: it is owned by uid "
            f"{st.st_uid}, not the current user (uid {os.getuid()}). {_FIX_HINT}"
        )
    if st.st_mode & 0o077:
        if repair_mode:
            os.chmod(path, _DIR_MODE)
            return
        raise CacheError(
            f"Refusing to use cache directory `{path}`: its mode "
            f"{oct(stat.S_IMODE(st.st_mode))} gives group/other access "
            f"(run `chmod 700 {path}`). {_FIX_HINT}"
        )


def _ensure_private_dir(path: pathlib.Path, *, repair_mode: bool = False) -> None:
    """Create ``path`` with mode 0700 if missing, then verify it is private."""
    try:
        os.mkdir(path, _DIR_MODE)
    except FileExistsError:
        pass
    else:
        # mkdir's mode is filtered by the umask; make it exactly 0700.
        os.chmod(path, _DIR_MODE)
    _verify_private_dir(path, repair_mode=repair_mode)


def get_cache_root() -> pathlib.Path:
    """Return the private runtime cache root, creating and verifying it.

    Raises CacheError if the root is a symlink, not a directory, owned by
    another user, or accessible to group/other.
    """
    configured = os.environ.get(_CACHE_ROOT_ENV, "").strip()
    if configured:
        root = pathlib.Path(os.path.abspath(os.path.expanduser(configured)))
        root.parent.mkdir(parents=True, exist_ok=True)
    else:
        root = pathlib.Path(os.path.abspath(default_cache_root()))
    _ensure_private_dir(root)
    return root


def _check_name(name: str) -> str:
    """Validate a single artifact file or directory name."""
    if (
        not isinstance(name, str)
        or name in (".", "..")
        or not _ARTIFACT_NAME_RE.fullmatch(name)
    ):
        raise CacheError(
            f"Invalid artifact name {name!r}: only letters, digits, '.', '_' "
            "and '-' are allowed."
        )
    return name


def _assert_direct_child(root: pathlib.Path, path: pathlib.Path) -> None:
    """Ensure ``path`` resolves to a direct child of ``root``."""
    if path.resolve().parent != root.resolve():
        raise CacheError(f"Artifact path `{path}` escapes the cache root `{root}`.")


def get_artifact_path(name: str) -> pathlib.Path:
    """Return the path of artifact ``name`` directly under the cache root.

    The file is not created; write it with :func:`write_artifact`.
    """
    root = get_cache_root()
    path = root / _check_name(name)
    _assert_direct_child(root, path)
    return path


def get_artifact_dir(name: str) -> pathlib.Path:
    """Create (mode 0700) and return artifact directory ``name`` under the root."""
    path = get_artifact_path(name)
    _ensure_private_dir(path, repair_mode=True)
    return path


def write_artifact(path: pathlib.Path, data: str | bytes) -> pathlib.Path:
    """Atomically write ``data`` to an artifact path with mode 0600.

    ``path`` must come from :func:`get_artifact_path`, or be a file inside a
    directory from :func:`get_artifact_dir`. The data goes to a fresh
    temp file opened with ``O_CREAT | O_EXCL | O_NOFOLLOW`` and is then
    renamed over ``path``; rename replaces a symlink rather than following it.
    """
    path = pathlib.Path(path)
    root = get_cache_root()
    _check_name(path.name)
    parent = path.parent
    if parent.resolve() != root.resolve():
        # One level of artifact directory is allowed (e.g. cached pages).
        _check_name(parent.name)
        _assert_direct_child(root, parent)
        _verify_private_dir(parent, repair_mode=True)

    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    tmp_path = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY
    fd = os.open(tmp_path, flags, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return path


def configure_process_cache_env() -> pathlib.Path:
    """Pin the cache root and common temp/cache env vars to the private root.

    Also pins ``GRADESCOPE_MCP_CACHE_DIR`` to the resolved root so later calls
    (and child processes) keep using the same directory after ``TMPDIR`` moves.
    """
    root = get_cache_root()
    os.environ[_CACHE_ROOT_ENV] = str(root)
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(root)
    # tempfile caches its directory; make Python temp files follow TMPDIR too.
    tempfile.tempdir = None

    xdg_cache_home = get_artifact_dir("xdg-cache")
    os.environ["XDG_CACHE_HOME"] = str(xdg_cache_home)
    return root
