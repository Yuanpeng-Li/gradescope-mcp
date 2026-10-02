"""Regression tests for the workflow-helper and artifact-cache review fixes.

Each test reproduces a verified defect (V4-x / V2-4 / NEW-V4) offline. HTTP
goes through a real ``requests.Session`` whose transport adapter is a fake
Gradescope, so session headers, cookies and redirects behave as in
production while nothing touches the network.
"""

from __future__ import annotations

import os
import stat
import tempfile

import pytest

from gradescope_mcp import cache

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


# ---------------------------------------------------------------------------
# V2-4 — private, verified cache root and safe artifact writes
# ---------------------------------------------------------------------------

def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_cache_root_and_files_are_private(tmp_path, monkeypatch) -> None:
    root = tmp_path / "fresh-root"
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))

    path = cache.write_artifact(cache.get_artifact_path("x.md"), "secret")
    page_dir = cache.get_artifact_dir("gradescope-pages-1-2-3")
    page = cache.write_artifact(page_dir / "page_1.jpg", JPEG)

    assert _mode(root) == 0o700
    assert _mode(page_dir) == 0o700
    assert _mode(path) == 0o600
    assert _mode(page) == 0o600
    assert path.read_text() == "secret"
    assert [p.name for p in root.iterdir() if p.name.endswith(".tmp")] == []


def test_new_cache_dirs_are_0700_under_any_umask(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(tmp_path / "root"))
    old_umask = os.umask(0o277)
    try:
        root = cache.get_cache_root()
        page_dir = cache.get_artifact_dir("gradescope-pages-1-2-3")
    finally:
        os.umask(old_umask)

    assert _mode(root) == 0o700
    assert _mode(page_dir) == 0o700


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o770])
def test_cache_refuses_root_accessible_to_others(tmp_path, monkeypatch, mode) -> None:
    root = tmp_path / "shared-root"
    root.mkdir()
    os.chmod(root, mode)
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))

    with pytest.raises(cache.CacheError, match="group/other access"):
        cache.get_cache_root()


def test_cache_refuses_symlinked_root(tmp_path, monkeypatch) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    link = tmp_path / "link-root"
    link.symlink_to(target)
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(link))

    with pytest.raises(cache.CacheError, match="symlink"):
        cache.get_cache_root()


def test_cache_refuses_root_owned_by_another_user(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))
    real_uid = os.getuid()
    monkeypatch.setattr(cache.os, "getuid", lambda: real_uid + 4242)

    with pytest.raises(cache.CacheError, match="not the current user"):
        cache.get_cache_root()


def test_planted_symlinks_are_never_followed(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))
    cache.get_cache_root()
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    planted = root / "gradescope-answerkey-77.md"
    planted.symlink_to(victim)

    # A link that escapes the root is refused when the path is looked up ...
    with pytest.raises(cache.CacheError, match="escapes"):
        cache.get_artifact_path("gradescope-answerkey-77.md")

    # ... and writing to the link path directly replaces the link, not its target.
    cache.write_artifact(planted, "# new")
    assert victim.read_text() == "ORIGINAL"
    assert not planted.is_symlink()
    assert planted.read_text() == "# new"
    assert _mode(planted) == 0o600


def test_planted_symlinked_page_dir_is_refused(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))
    cache.get_cache_root()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "gradescope-pages-1-2-3").symlink_to(elsewhere)

    with pytest.raises(cache.CacheError):
        cache.get_artifact_dir("gradescope-pages-1-2-3")
    with pytest.raises(cache.CacheError):
        cache.write_artifact(root / "gradescope-pages-1-2-3" / "page_1.jpg", JPEG)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "../escape", "x y", "page_1.jpg/..", "évil"])
def test_artifact_names_are_validated(name) -> None:
    with pytest.raises(cache.CacheError):
        cache.get_artifact_path(name)


def test_write_artifact_refuses_paths_outside_the_root(tmp_path) -> None:
    with pytest.raises(cache.CacheError):
        cache.write_artifact(tmp_path / "outside" / "x.md", "x")


def test_default_root_prefers_private_runtime_dir(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "run-user"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert cache.default_cache_root() == runtime / "gradescope-mcp"

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert cache.default_cache_root() == tmp_path / f"gradescope-mcp-{os.getuid()}"

    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.delenv("GRADESCOPE_MCP_CACHE_DIR")
    root = cache.get_cache_root()
    assert root == tmp_path / f"gradescope-mcp-{os.getuid()}"
    assert _mode(root) == 0o700


def test_configure_process_cache_env_pins_private_root(tmp_path, monkeypatch) -> None:
    for key in ("TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, "unchanged")
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    root = tmp_path / "pinned"
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))

    assert cache.configure_process_cache_env() == root
    assert os.environ["GRADESCOPE_MCP_CACHE_DIR"] == str(root)
    assert os.environ["TMPDIR"] == str(root)
    assert os.environ["XDG_CACHE_HOME"] == str(root / "xdg-cache")
    assert _mode(root / "xdg-cache") == 0o700
    assert tempfile.gettempdir() == str(root)


# ---------------------------------------------------------------------------
# V4-12 — the suite itself is hermetic
# ---------------------------------------------------------------------------

def test_suite_uses_a_per_test_cache(tmp_path) -> None:
    root = cache.get_cache_root()
    assert root.is_relative_to(tmp_path)
    assert "GRADESCOPE_EMAIL" not in os.environ
    assert "GRADESCOPE_PASSWORD" not in os.environ
