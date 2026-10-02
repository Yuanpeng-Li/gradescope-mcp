"""Round 3 (unit G3): startup .env robustness, the documented second-expiry
results, the per-page download deadline, and regrade completion icons."""

from __future__ import annotations

import gzip
import io
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
import requests
from bs4 import BeautifulSoup
from requests.models import Response
from requests.structures import CaseInsensitiveDict

import gradescope_mcp.__main__ as entry
from gradescope_mcp import auth, server
from gradescope_mcp.tools import grading_workflow as gw
from gradescope_mcp.tools import regrades

ROOT = Path(__file__).resolve().parent.parent
POSIX_NON_ROOT = pytest.mark.skipif(
    not hasattr(os, "getuid") or os.getuid() == 0,
    reason="needs POSIX permissions enforced for a non-root user",
)


def _write(path: Path, text: str | bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    path.chmod(mode)
    return path


def _checkout(root: Path) -> Path:
    """A gradescope-mcp source checkout layout; returns its package directory."""
    _write(root / "pyproject.toml", '[project]\nname = "gradescope-mcp"\n')
    package = root / "src" / "gradescope_mcp"
    package.mkdir(parents=True)
    return package


# ---------------------------------------------------------------------------
# [4] .env discovery never stops the server from starting
# ---------------------------------------------------------------------------


def _run_main(cwd: Path, tmp_path: Path, *, delete_cwd: bool = False) -> subprocess.CompletedProcess:
    """Run the real ``main()`` (with ``MCPServer.run`` stubbed) in a fresh
    interpreter, from ``cwd``; ``delete_cwd`` removes it before ``main()``."""
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    env = {
        k: v for k, v in os.environ.items()
        if k.lower() not in ("pythonpath", "gs_t_unreadable")
    }
    env["GRADESCOPE_MCP_CACHE_DIR"] = str(cache)
    script = (
        "import os, sys\n"
        "from mcp.server.mcpserver import MCPServer\n"
        "MCPServer.run = lambda self, *a, **k: print('SERVER RUN', file=sys.stderr)\n"
        "import gradescope_mcp.__main__ as m\n"
        "if sys.argv[1] == 'delete':\n"
        "    os.rmdir(os.getcwd())\n"
        "m.main()\n"
        "print('UNREADABLE=' + repr(os.environ.get('GS_T_UNREADABLE')))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script, "delete" if delete_cwd else "keep"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )


def test_server_starts_when_the_working_directory_was_deleted(tmp_path) -> None:
    """Reviewer repro Q2/entry.py (a): run from a directory removed after the
    client started. Before the fix ``Path.cwd()`` raised FileNotFoundError
    out of ``main()``."""
    gone = tmp_path / "deleted-worktree"
    gone.mkdir()

    proc = _run_main(gone, tmp_path, delete_cwd=True)

    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    assert (
        "Ignoring .env: the working directory is unavailable (No such file or directory)."
        in proc.stderr
    )
    assert "Using private runtime cache directory" in proc.stderr
    assert "SERVER RUN" in proc.stderr


@POSIX_NON_ROOT
def test_server_starts_when_the_cwd_dotenv_cannot_be_read(tmp_path) -> None:
    """Reviewer repro Q2 (b): a .env that stats fine but cannot be opened
    raised PermissionError out of ``main()``; it is now skipped with a
    warning and the server starts."""
    work = tmp_path / "work"
    dotenv = _write(work / ".env", "GS_T_UNREADABLE=1\n", mode=0o000)

    proc = _run_main(work, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert f"Ignoring {dotenv}: cannot be read (Permission denied)." in proc.stderr
    assert "SERVER RUN" in proc.stderr
    assert "UNREADABLE=None" in proc.stdout


def test_unavailable_cwd_skips_only_the_cwd_dotenv(tmp_path, monkeypatch) -> None:
    package = _checkout(tmp_path / "checkout")
    checkout_env = _write(tmp_path / "checkout" / ".env", "GS_T_FROM=checkout\n")
    monkeypatch.setenv("GS_T_FROM", "")
    monkeypatch.delenv("GS_T_FROM")

    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(entry.Path, "cwd", staticmethod(gone))

    loaded, skipped = entry.load_env_files(package_dir=package)

    assert loaded == [checkout_env]
    assert skipped == [
        (Path(".env"), "the working directory is unavailable (No such file or directory)")
    ]
    assert os.environ["GS_T_FROM"] == "checkout"


@POSIX_NON_ROOT
def test_unreadable_dotenv_is_skipped_and_the_next_one_still_loads(tmp_path, monkeypatch) -> None:
    package = _checkout(tmp_path / "checkout")
    checkout_env = _write(tmp_path / "checkout" / ".env", "GS_T_A=checkout\n")
    work = tmp_path / "work"
    unreadable = _write(work / ".env", "GS_T_A=cwd\n", mode=0o000)
    monkeypatch.setenv("GS_T_A", "")
    monkeypatch.delenv("GS_T_A")

    loaded, skipped = entry.load_env_files(cwd=work, package_dir=package)

    assert loaded == [checkout_env]
    assert skipped == [(unreadable, "cannot be read (Permission denied)")]
    assert os.environ["GS_T_A"] == "checkout"


def _root_owned_unreadable_file() -> Path | None:
    if not hasattr(os, "getuid") or os.getuid() == 0:
        return None
    for name in ("/etc/sudoers", "/etc/shadow", "/etc/gshadow", "/etc/master.passwd"):
        path = Path(name)
        try:
            if path.stat().st_uid == 0 and path.is_file() and not os.access(path, os.R_OK):
                return path
        except OSError:
            continue
    return None


@pytest.mark.skipif(_root_owned_unreadable_file() is None, reason="no root-owned unreadable file")
def test_root_owned_unreadable_dotenv_is_skipped(tmp_path) -> None:
    """Reviewer repro Q2/rootenv: ``.env -> /etc/sudoers`` stands in for a
    root-owned 0600 .env, which the ownership rule allows."""
    work = tmp_path / "rootenv"
    work.mkdir()
    (work / ".env").symlink_to(_root_owned_unreadable_file())

    assert entry._untrusted_reason(work / ".env") is None  # ownership is fine
    loaded, skipped = entry.load_env_files(cwd=work, package_dir=tmp_path / "pkg")

    assert loaded == []
    assert skipped == [(work / ".env", "cannot be read (Permission denied)")]


@POSIX_NON_ROOT
def test_dotenv_in_an_unsearchable_directory_is_reported_not_raised(tmp_path) -> None:
    """``stat`` of the .env fails with EACCES, which ``Path.is_file`` re-raises."""
    work = tmp_path / "work"
    _write(work / ".env", "GS_T_HIDDEN=1\n")
    work.chmod(0o600)  # no search permission
    try:
        loaded, skipped = entry.load_env_files(cwd=work, package_dir=tmp_path / "pkg")
    finally:
        work.chmod(0o700)

    assert loaded == []
    assert skipped == [(work / ".env", "cannot be read (Permission denied)")]


def test_undecodable_dotenv_is_skipped_without_partial_load(tmp_path, monkeypatch) -> None:
    work = tmp_path / "work"
    dotenv = _write(work / ".env", b"GS_T_PART=1\n\xff\xfe\xfa\n")
    monkeypatch.setenv("GS_T_PART", "")
    monkeypatch.delenv("GS_T_PART")

    loaded, skipped = entry.load_env_files(cwd=work, package_dir=tmp_path / "pkg")

    assert loaded == []
    assert skipped == [(dotenv, "it is not valid UTF-8 text")]
    assert "GS_T_PART" not in os.environ


def test_module_docstring_promises_startup_never_fails_on_dotenv() -> None:
    doc = " ".join(entry.__doc__.split())
    assert "never stop the server from starting" in doc
