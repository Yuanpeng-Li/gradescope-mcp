"""Entry point for the Gradescope MCP server.

Usage:
    uv run python -m gradescope_mcp

Configuration comes from environment variables. Before anything reads them,
``.env`` files are loaded with python-dotenv, never overriding a variable
that is already set. Precedence, highest first:

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
"""

import io
import logging
import os
import re
import stat
from pathlib import Path

from dotenv import load_dotenv

from gradescope_mcp.cache import CacheError, configure_process_cache_env

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
    cwd: Path | None = None, package_dir: Path | None = None
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Load the ``.env`` files described in the module docstring.

    Returns the files loaded and the files skipped (with the reason), in
    precedence order. Variables already in the environment are kept.
    """
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
    return loaded, skipped


def main():
    # Start-up runs here rather than at import time, so importing this module
    # (e.g. for ``load_env_files``) changes nothing. Both ``python -m
    # gradescope_mcp`` and the ``gradescope-mcp`` console script call main().

    # Load environment variables from .env files before anything reads them.
    dotenv_loaded, dotenv_skipped = load_env_files()

    # An unsafe cache root (symlink, foreign owner, group/other access) must
    # not keep the server from starting: most tools never touch the cache, and
    # the workflow tools re-check the root and report the problem on every use.
    try:
        cache_root = configure_process_cache_env()
        cache_error = None
    except (CacheError, OSError) as e:
        cache_root = None
        cache_error = e

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)
    for path in dotenv_loaded:
        logger.info("Loaded environment defaults from %s", path)
    for path, reason in dotenv_skipped:
        logger.warning("Ignoring %s: %s.", path, reason)
    if cache_root is not None:
        logger.info("Using private runtime cache directory: %s", cache_root)
    else:
        logger.error(
            "Runtime cache directory is unavailable: %s. Tools that write "
            "artifacts will fail until this is fixed.",
            cache_error,
        )
    from gradescope_mcp.server import mcp
    mcp.run()


if __name__ == "__main__":
    main()
