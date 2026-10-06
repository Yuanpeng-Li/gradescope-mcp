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


# ---------------------------------------------------------------------------
# [5] The documented results of a second expiry match the code
# ---------------------------------------------------------------------------


_EXPIRED = object()  # stands in for the connection whose session expired


def _scripted_tool(runs):
    """A tool wrapped like ``gs_tool``; each run is ``(accepted writes,
    returned text or raised exception)`` and ends in an expiry."""
    script = iter(runs)

    def tool() -> str:
        writes, outcome = next(script)
        auth._local.expired = _EXPIRED
        auth._local.writes = writes
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return server._signal_errors(auth.with_session_recovery(tool))


def _text_and_error(result) -> tuple[str, bool]:
    if isinstance(result, str):
        return result, False
    return "\n".join(c.text for c in result.content), bool(result.is_error)


def test_second_expiry_after_a_write_in_the_rerun_returns_its_output_and_notice() -> None:
    """Reviewer repro R2/double_expiry.py: the re-run saved row 5, then
    expired again. The result is the re-run's output plus the writes notice,
    and it is not an error result."""
    tool = _scripted_tool([
        (0, "## Batch grade result\n- **succeeded:** 0"),
        (1, "## Batch grade result\n- **succeeded:** 1\n### Saved\n- `5`"),
    ])

    text, is_error = _text_and_error(tool())

    assert not is_error
    assert text.startswith("## Batch grade result\n- **succeeded:** 1")
    assert text.endswith(auth._writes_then_expiry_notice(1))


def test_second_expiry_after_a_write_in_a_rerun_that_raised_is_an_error() -> None:
    tool = _scripted_tool([
        (0, "first"),
        (1, auth.SessionExpiredError("expired")),
    ])

    text, is_error = _text_and_error(tool())

    assert is_error
    assert text.startswith(
        "Authentication error: Gradescope session expired during the call after "
        "Gradescope had accepted 1 write request(s)"
    )


def test_second_expiry_without_writes_is_the_recovery_error_plus_an_output() -> None:
    text, is_error = _text_and_error(_scripted_tool([(0, "first"), (0, "second")])())
    assert is_error and text.startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
    assert "Output of the first attempt" in text and text.endswith("first")

    # The first attempt raised: the re-run's output follows the error.
    text, is_error = _text_and_error(
        _scripted_tool([(0, auth.SessionExpiredError("x")), (0, "second")])()
    )
    assert is_error and text.startswith(auth.SESSION_RECOVERY_FAILED_MESSAGE)
    assert "Output of the retry" in text and text.endswith("second")

    # Neither returned text: the error stands alone.
    text, is_error = _text_and_error(_scripted_tool(
        [(0, auth.SessionExpiredError("x")), (0, auth.SessionExpiredError("y"))]
    )())
    assert is_error and text == auth.SESSION_RECOVERY_FAILED_MESSAGE


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end, text.index(start))]


def test_readme_states_both_second_expiry_results() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _flat(_section(readme, "## Authentication", "## Architecture"))

    assert "If the session expires again during the re-run" in section
    # Case 1: a write accepted in the re-run.
    assert (
        "the re-run's output is returned with the same \"⚠️ ... accepted N write "
        "request(s) ...\" notice. This is not an error result" in section
    )
    # Case 2: no write in the re-run.
    assert (
        f"`{auth.SESSION_RECOVERY_FAILED_MESSAGE}` (`isError: true`), followed by "
        "the output of the first attempt (or of the re-run, if the first attempt "
        "raised)" in section
    )
    assert "followed by the output of the first attempt, labelled" not in section


def test_agent_md_states_both_second_expiry_results() -> None:
    agent = _flat((ROOT / "AGENT.md").read_text(encoding="utf-8"))

    assert "A second expiry returns `SESSION_RECOVERY_FAILED_MESSAGE` followed by the first" not in agent
    assert "If the re-run expires too, a re-run that had a write accepted is reported the same way" in agent
    assert "not an error result (`isError` false)" in agent
    assert "A re-run that wrote nothing returns `SESSION_RECOVERY_FAILED_MESSAGE`" in agent


# ---------------------------------------------------------------------------
# [9] The per-page deadline bounds the whole download, not each 64 KiB chunk
# ---------------------------------------------------------------------------


JPEG = b"\xff\xd8\xff\xe0"


@pytest.fixture
def local_http(monkeypatch):
    """Start local 127.0.0.1 HTTP servers whose responses are scripted.

    ``local_http(handler)`` returns the base URL; ``handler(conn, stop)``
    writes the whole response to the client socket.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    stop = threading.Event()
    listeners: list[socket.socket] = []

    def start(handler) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.1)
        listeners.append(listener)

        def handle(conn):
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    data = conn.recv(65536)
                    if not data:
                        return
                    request += data
                try:
                    handler(conn, stop)
                except OSError:
                    pass  # the client gave up and closed the connection

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                threading.Thread(target=handle, args=(conn,), daemon=True).start()

        threading.Thread(target=serve, daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    yield start
    stop.set()
    for listener in listeners:
        listener.close()


def _client() -> requests.Session:
    """A session with the transport the real connection mounts (10 s / 60 s)."""
    session = requests.Session()
    session.trust_env = False
    adapter = auth.TimeoutHTTPAdapter()
    session.mount("http://", adapter)
    return session


def _head(*extra: str) -> bytes:
    lines = ["HTTP/1.1 200 OK", "Content-Type: image/jpeg", "Connection: close", *extra]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _drip(conn, stop, byte: bytes) -> None:
    """One byte every 50 ms for up to 30 s: each socket read returns well
    within the 60 s read timeout, so only the page deadline can stop it."""
    end = time.monotonic() + 30
    while not stop.is_set() and time.monotonic() < end:
        conn.sendall(byte)
        stop.wait(0.05)


def _trickle(conn, stop):  # reviewer repro Q4/drip.py
    conn.sendall(_head() + JPEG)
    _drip(conn, stop, b"0")


def _trickle_with_length(conn, stop):
    conn.sendall(_head("Content-Length: 100000") + JPEG)
    _drip(conn, stop, b"0")


def _stall(conn, stop):
    """Part of the body, then nothing: one read blocks for the read timeout."""
    conn.sendall(_head("Content-Length: 100000") + JPEG)
    stop.wait(30)


def _chunk_size_trickle(conn, stop):
    """A trickle into a chunk-size line: ``read1`` blocks in ``readline``."""
    conn.sendall(_head("Transfer-Encoding: chunked") + b"4\r\n" + JPEG + b"\r\n")
    _drip(conn, stop, b"0")


def _gzip_header_trickle(conn, stop):
    """A trickle into a gzip comment: the decoder yields nothing, so urllib3's
    ``read1`` keeps reading without returning to the caller."""
    header = b"\x1f\x8b\x08\x10" + b"\x00" * 4 + b"\x00\xff"  # FCOMMENT set
    conn.sendall(_head("Content-Encoding: gzip") + header)
    _drip(conn, stop, b"a")


@pytest.mark.parametrize(
    "handler",
    [_trickle, _trickle_with_length, _stall, _chunk_size_trickle, _gzip_header_trickle],
    ids=["trickle", "trickle-with-length", "stall", "chunk-size-trickle", "gzip-header-trickle"],
)
def test_slow_page_is_stopped_at_the_deadline(monkeypatch, local_http, handler) -> None:
    """Before the fix the deadline was checked only after a full 64 KiB chunk,
    so these ran until the server closed the connection (30 s here; for ever
    in the worst case) instead of stopping after the 0.5 s budget."""
    monkeypatch.setattr(gw, "_PAGE_DEADLINE_SECONDS", 0.5)
    url = local_http(handler) + "/p1.jpg"

    started = time.monotonic()
    with pytest.raises(gw._PageFetchError, match=r"^download took longer than 0\.5 s \(\d+ bytes received\); stopped$"):
        gw._download_page_image(_client(), url)

    assert time.monotonic() - started < 5


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_stalled_https_page_is_interrupted_at_the_deadline(monkeypatch, tmp_path) -> None:
    """Gradescope and S3 serve pages over TLS: the watchdog's socket
    shutdown must also wake a read blocked inside the TLS layer."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    made = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        capture_output=True, timeout=60,
    )
    if made.returncode != 0:
        pytest.skip("openssl could not create a test certificate")
    monkeypatch.setattr(gw, "_PAGE_DEADLINE_SECONDS", 0.5)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()

    def serve():
        try:
            raw, _ = listener.accept()
            with context.wrap_socket(raw, server_side=True) as conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(65536)
                conn.sendall(_head("Content-Length: 100000") + JPEG)
                stop.wait(30)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    session = requests.Session()
    session.trust_env = False
    session.verify = str(cert)
    session.mount("https://", auth.TimeoutHTTPAdapter())
    started = time.monotonic()
    try:
        with pytest.raises(gw._PageFetchError, match=r"took longer than 0\.5 s \(4 bytes received\)"):
            gw._download_page_image(session, f"https://127.0.0.1:{listener.getsockname()[1]}/p1.jpg")
    finally:
        stop.set()
        listener.close()
    assert time.monotonic() - started < 5


BODY = JPEG + os.urandom(300_000)


def _full(conn, _stop):
    conn.sendall(_head(f"Content-Length: {len(BODY)}") + BODY)


def _until_close(conn, _stop):
    conn.sendall(_head() + BODY)


def _chunked(conn, _stop):
    conn.sendall(_head("Transfer-Encoding: chunked"))
    step = 70_001
    for start in range(0, len(BODY), step):
        part = BODY[start:start + step]
        conn.sendall(f"{len(part):x}\r\n".encode() + part + b"\r\n")
    conn.sendall(b"0\r\n\r\n")


def _gzipped(conn, _stop):
    payload = gzip.compress(BODY)
    conn.sendall(_head("Content-Encoding: gzip", f"Content-Length: {len(payload)}") + payload)


@pytest.mark.parametrize(
    "handler", [_full, _until_close, _chunked, _gzipped],
    ids=["content-length", "until-close", "chunked", "gzip"],
)
def test_page_bodies_are_still_read_in_full(local_http, handler) -> None:
    data, extension = gw._download_page_image(_client(), local_http(handler) + "/p1.jpg")
    assert data == BODY and extension == "jpg"


def test_finished_pages_leave_the_connection_reusable(monkeypatch) -> None:
    """Reading the body with ``read1`` still releases a keep-alive
    connection to the pool, so pages on one host share a connection."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(5)
    connections: list[socket.socket] = []

    def handle(conn):
        pending = b""
        with conn:
            while True:
                while b"\r\n\r\n" not in pending:
                    data = conn.recv(65536)
                    if not data:
                        return
                    pending += data
                pending = pending.split(b"\r\n\r\n", 1)[1]
                head = "HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n"
                conn.sendall(head.format(len(BODY)).encode() + BODY)

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            connections.append(conn)
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    try:
        session = _client()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/p.jpg"
        for _ in range(3):
            assert gw._download_page_image(session, url) == (BODY, "jpg")
        assert len(connections) == 1
    finally:
        listener.close()


def test_oversized_body_over_a_socket_is_still_cut_off(monkeypatch, local_http) -> None:
    monkeypatch.setattr(gw, "_MAX_PAGE_BYTES", 256 * 1024)

    def huge(conn, _stop):
        conn.sendall(_head() + JPEG + b"\x00" * (8 * 1024 * 1024))

    with pytest.raises(gw._PageFetchError, match="too large"):
        gw._download_page_image(_client(), local_http(huge) + "/p1.jpg")


def test_deadline_counts_the_time_until_the_headers_arrive(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(gw, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class SlowHeaders:
        def get(self, url, stream=False, **_kwargs):
            clock[0] += gw._PAGE_DEADLINE_SECONDS + 1  # headers took too long
            resp = Response()
            resp.status_code = 200
            resp.raw = io.BytesIO(JPEG + b"\x00" * 10)
            resp.headers = CaseInsensitiveDict({"Content-Type": "image/jpeg"})
            return resp

    with pytest.raises(gw._PageFetchError, match="took longer than 120 s"):
        gw._download_page_image(SlowHeaders(), "https://s3.example/p1.jpg")


def test_watchdog_interrupts_once_and_never_after_stop() -> None:
    fired = threading.Event()
    resp = SimpleNamespace(raw=SimpleNamespace(shutdown=fired.set))

    with gw._ReadWatchdog(resp, time.monotonic() + 0.05) as watchdog:
        assert fired.wait(5)
    assert watchdog.stop() is True

    late = threading.Event()
    resp = SimpleNamespace(raw=SimpleNamespace(shutdown=late.set))
    with gw._ReadWatchdog(resp, time.monotonic() + 0.05) as watchdog:
        pass  # the read finished before the deadline
    assert not late.wait(0.3)
    assert watchdog.stop() is False


def test_cache_tool_drops_a_stalled_page_on_time_and_keeps_the_rest(monkeypatch, local_http) -> None:
    monkeypatch.setattr(gw, "_PAGE_DEADLINE_SECONDS", 0.5)
    stalled, good = local_http(_stall), local_http(_full)
    props = {
        "question": {"parameters": {"crop_rect_list": []}},
        "submission": {},
        "pages": [
            {"number": 1, "url": f"{stalled}/p1.jpg"},
            {"number": 2, "url": f"{good}/p2.jpg"},
        ],
    }
    conn = SimpleNamespace(session=_client(), gradescope_base_url="https://www.gradescope.com")
    monkeypatch.setattr(gw, "_resolve_assignment_questions", lambda *_a: ("7", {}, None))
    monkeypatch.setattr(gw, "_get_grading_context", lambda *_a: {"props": props})
    monkeypatch.setattr(gw, "get_connection", lambda: conn)

    started = time.monotonic()
    result = anyio.run(
        server.mcp.call_tool,
        "tool_cache_relevant_pages",
        {"course_id": "1", "assignment_id": "7", "question_id": "11", "submission_id": "21"},
    )
    text = "\n".join(c.text for c in result.content)

    assert time.monotonic() - started < 5
    assert text.startswith("Cached 1 of 2 relevant page(s)")
    assert re.search(r"page 1: download took longer than 0\.5 s \(\d+ bytes received\); stopped", text)
    assert "page_2.jpg" in text


def _hooked_client(base_url: str) -> requests.Session:
    """A client whose session carries the real expiry hook for ``base_url``
    (a Gradescope-hosted page URL goes through that session)."""
    conn = SimpleNamespace(session=_client(), gradescope_base_url=base_url)
    auth._install_expiry_hook(conn)
    return conn.session


def _html_head(*extra: str) -> bytes:
    lines = ["HTTP/1.1 200 OK", "Content-Type: text/html; charset=utf-8", "Connection: close", *extra]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def test_same_site_image_labelled_html_is_still_sniffed(local_http) -> None:
    """Round-4 C10 (reviewer repro round3-G3/hook_cases.py /mislabeled): the
    expiry hook read the body of a same-site text/html answer, the read1
    loop then read the drained stream, and a JPEG was rejected as 'not an
    image'."""
    def mislabeled(conn, _stop):
        conn.sendall(_html_head(f"Content-Length: {len(BODY)}") + BODY)

    base = local_http(mislabeled)
    assert gw._download_page_image(_hooked_client(base), base + "/p1.jpg") == (BODY, "jpg")


def test_body_already_read_by_a_hook_is_taken_from_content(local_http) -> None:
    """Round-4 C10: any hook that reads ``response.content`` drains the raw
    stream; the page is then taken from the content it read."""
    session = _client()

    def read_body(response, *_args, **_kwargs):
        response.content  # noqa: B018 - drains response.raw

    session.hooks["response"].append(read_body)

    data, extension = gw._download_page_image(session, local_http(_full) + "/p1.jpg")

    assert data == BODY and extension == "jpg"


def test_same_site_html_page_is_bounded_by_the_deadline_and_the_cap(monkeypatch, local_http) -> None:
    """Round-4 C13 (reviewer repro round3-G3/hook_cases.py /htmldrip): the
    expiry hook read a same-site HTML body in full inside session.get,
    before the deadline or the size cap applied (6 s for a 1 s budget)."""
    monkeypatch.setattr(gw, "_PAGE_DEADLINE_SECONDS", 0.5)

    def html_drip(conn, stop):
        conn.sendall(_html_head() + b"<html>")
        _drip(conn, stop, b" ")

    base = local_http(html_drip)
    started = time.monotonic()
    with pytest.raises(gw._PageFetchError, match=r"took longer than 0\.5 s"):
        gw._download_page_image(_hooked_client(base), base + "/p1.jpg")
    assert time.monotonic() - started < 5

    monkeypatch.setattr(gw, "_MAX_PAGE_BYTES", 256 * 1024)

    def huge_html(conn, _stop):
        conn.sendall(_html_head() + b"<html>" + b"x" * (8 * 1024 * 1024))

    base = local_http(huge_html)
    with pytest.raises(gw._PageFetchError, match="too large"):
        gw._download_page_image(_hooked_client(base), base + "/p1.jpg")


def test_same_site_logged_out_page_is_still_a_session_expiry(monkeypatch, local_http) -> None:
    """The hook leaves a streamed page's logged-out check to the reader,
    which still raises SessionExpiredError and flags the thread."""
    monkeypatch.setattr(auth._local, "expired", None, raising=False)
    monkeypatch.setattr(auth._local, "pending_write", False, raising=False)
    page = (
        b'<html><form action="/login" method="post">'
        b'<input type="hidden" name="authenticity_token" value="T"></form></html>'
    )

    def logged_out(conn, _stop):
        conn.sendall(_html_head(f"Content-Length: {len(page)}") + page)

    base = local_http(logged_out)
    with pytest.raises(auth.SessionExpiredError, match="logged-out page"):
        gw._download_page_image(_hooked_client(base), base + "/p1.jpg")
    assert auth._local.expired is not None

    # A logged-in HTML page (logout link) is only "not an image".
    monkeypatch.setattr(auth._local, "expired", None)
    logged_in = page.replace(b"</form>", b'</form><a href="/logout">Log out</a>')

    def html_page(conn, _stop):
        conn.sendall(_html_head(f"Content-Length: {len(logged_in)}") + logged_in)

    base = local_http(html_page)
    with pytest.raises(gw._PageFetchError, match="not an image"):
        gw._download_page_image(_hooked_client(base), base + "/p1.jpg")
    assert auth._local.expired is None


@pytest.mark.parametrize("handler", [_full, _chunked, _gzipped], ids=["content-length", "chunked", "gzip"])
def test_urllib3_without_read1_falls_back_to_iter_content(monkeypatch, local_http, handler) -> None:
    """Round-4 C8 (reviewer repro round3-G3/dl_cases.py with urllib3 1.26):
    HTTPResponse.read1 (urllib3 2.2) and shutdown() (2.3) are missing on an
    older urllib3, and every page failed with AttributeError."""
    from urllib3.response import HTTPResponse

    for cls in HTTPResponse.__mro__:
        for name in ("read1", "shutdown"):
            if name in vars(cls):
                monkeypatch.delattr(cls, name)
    assert not hasattr(HTTPResponse, "read1")

    data, extension = gw._download_page_image(_client(), local_http(handler) + "/p1.jpg")

    assert data == BODY and extension == "jpg"


def test_pyproject_declares_the_urllib3_the_page_reader_needs() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '"urllib3>=2.3"' in pyproject


# ---------------------------------------------------------------------------
# [10] Regrade completion: only specific, visible evidence is ✅
# ---------------------------------------------------------------------------


def _regrade_rows(cells: list[str]) -> str:
    rows = "".join(
        f"<tr><td>S{i}</td><td>1.1</td><td>TA</td><td>{cell}</td>"
        f"<td><a href='/courses/1/questions/11/submissions/{i}/grade'>Review</a></td></tr>"
        for i, cell in enumerate(cells, 1)
    )
    return (
        "<html><title>Regrade Requests</title><table><thead><tr><th>Student</th>"
        "<th>Question</th><th>Grader</th><th>Completed</th><th></th></tr></thead>"
        f"<tbody>{rows}</tbody></table></html>"
    )


def _regrades(monkeypatch, cells: list[str]) -> str:
    page = _regrade_rows(cells)
    session = SimpleNamespace(get=lambda url, **_kw: SimpleNamespace(status_code=200, text=page))
    conn = SimpleNamespace(gradescope_base_url="https://gs.test", session=session)
    monkeypatch.setattr(regrades, "get_connection", lambda: conn)
    return regrades.get_regrade_requests("1", "2")


@pytest.mark.parametrize("cell, status", [
    # Reviewer probe Q4/icons2.py: each of these was ✅ (or ❓ for the checked input).
    ('<i class="fa fa-check d-none"></i>', "❓"),
    ('<i class="fa fa-check" style="display:none"></i>', "❓"),
    ('<i class="fa fa-check" hidden></i>', "❓"),
    ('<span class="check"></span>', "❓"),
    ('<input type="checkbox" class="check">', "⏳"),
    ('<input type="checkbox" checked>', "✅"),
    # Hidden by an enclosing element or by visibility.
    ('<span class="d-none"><i class="fa fa-check"></i></span>', "❓"),
    ('<i class="fa fa-check" style="visibility: hidden"></i>', "❓"),
    ('<i class="fa fa-check invisible"></i>', "❓"),
    # Generic CSS-checkbox classes are not check icons.
    ('<span class="checkmark"></span>', "❓"),
    ('<span class="check-mark"></span>', "❓"),
    # Greyed-out or screen-reader-only check icons are not visible evidence.
    ('<i class="fa fa-check text-muted"></i>', "❓"),
    ('<span class="is-disabled"><i class="fa fa-check"></i></span>', "❓"),
    ('<svg class="icon--inactive"><use href="#check"></use></svg>', "❓"),
    ('<i class="fa fa-check sr-only"></i>', "❓"),
    # Checkboxes are read by their state only.
    ('<input type="checkbox" class="sr-only" checked><span class="checkmark"></span>', "✅"),
    ('<input type="checkbox" class="d-none" checked><span class="check"></span>', "❓"),
    # A label contradicting the state is conflicting evidence (round-4 C9).
    ('<label><input type="checkbox"> Completed</label>', "❓"),
    ('<div role="checkbox" aria-checked="true"></div>', "✅"),
    ('<div role="checkbox" aria-checked="false"></div>', "⏳"),
    ('<input type="checkbox" checked><input type="checkbox">', "❓"),
    # Hidden text and labels are ignored; sr-only text still counts.
    ('<span class="d-none">Completed</span>', "❓"),
    ('<span class="d-none">Completed</span><span>Pending</span>', "⏳"),
    ('<i class="fa fa-check d-none" title="Completed"></i>', "❓"),
    ('<i class="fa fa-check" aria-hidden="true"></i><span class="sr-only">Completed</span>', "✅"),
    # Visible, specific evidence is still ✅.
    ('<i class="fa fa-check"></i>', "✅"),
    ('<i class="fa fa-check" aria-hidden="true"></i>', "✅"),
    ('<svg><use href="/icons.svg#check"></use></svg>', "✅"),
    ('<span class="glyphicon glyphicon-ok"></span>', "✅"),
    ("", "⏳"),
])
def test_regrade_completion_needs_specific_visible_evidence(monkeypatch, cell, status) -> None:
    out = _regrades(monkeypatch, [cell])
    assert f"| 1 | {status} | S1 | 1.1 | TA | qid=11, sid=1 |" in out


def test_same_check_element_hidden_for_open_requests_does_not_hide_them(monkeypatch) -> None:
    """The finding's scenario: every row renders the same check element and
    open requests hide it. Before the fix all three rows were ✅ and the
    review prompt would skip the two open requests."""
    out = _regrades(monkeypatch, [
        '<i class="fa fa-check"></i>',
        '<i class="fa fa-check d-none"></i>',
        '<i class="fa fa-check" style="display: none"></i>',
    ])
    assert "**Pending:** 0 | **Completed:** 1 | **Unknown:** 2 | **Total:** 3" in out

    out = _regrades(monkeypatch, ['<span class="check"></span>'] * 3)
    assert "**Completed:** 0" in out and "**Unknown:** 3" in out


@pytest.mark.parametrize("cell, status", [
    # Reviewer probe round3-G3/icons3.py: a checked box next to visible
    # "Pending" was ✅, the one status the review prompt skips.
    ('<input type="checkbox" checked> Pending', "❓"),
    ('<input type="checkbox" checked><span>Not completed</span>', "❓"),
    ('<div role="checkbox" aria-checked="true"></div> pending', "❓"),
    ('<input type="checkbox" checked title="Not completed">', "❓"),
    ('<input type="checkbox"> Completed', "❓"),
    ('<input type="checkbox"> Oct 3, 2026', "❓"),
    # Agreeing or neutral text keeps the checkbox's reading.
    ('<input type="checkbox" checked> Completed', "✅"),
    ('<input type="checkbox" checked> Oct 3, 2026', "✅"),
    ('<input type="checkbox"> Pending', "⏳"),
    ('<label><input type="checkbox" checked> Mark as resolved</label>', "✅"),
    ('<label><input type="checkbox"> Mark as resolved</label>', "⏳"),
    # Hidden contradicting text is not evidence.
    ('<input type="checkbox" checked><span class="d-none">Pending</span>', "✅"),
])
def test_checkbox_contradicted_by_visible_text_is_unknown(monkeypatch, cell, status) -> None:
    out = _regrades(monkeypatch, [cell])
    assert f"| 1 | {status} | S1 | 1.1 | TA | qid=11, sid=1 |" in out


def test_regrade_tool_description_matches_the_classifier(monkeypatch) -> None:
    """Round-4 C11: the MCP-visible description still said only an empty
    cell (or a pending word) is pending."""
    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    doc = _flat(tools["tool_get_regrade_requests"].description)
    assert "only an empty cell" not in doc
    assert "⏳ pending is an unchecked checkbox, a pending word, or an empty cell" in doc
    assert "a generic ``check`` class" in doc
    assert "a checkbox whose state contradicts the cell's visible text" in doc
    out = _regrades(monkeypatch, [
        '<input type="checkbox">', '<span class="check"></span>',
        '<input type="checkbox" checked> Pending',
    ])
    assert "**Pending:** 1 | **Completed:** 0 | **Unknown:** 2 | **Total:** 3" in out


def test_classifying_a_cell_does_not_change_the_page() -> None:
    soup = BeautifulSoup(
        '<table><tr><td><i class="fa fa-check d-none"></i><span class="d-none">x</span></td></tr></table>',
        "html.parser",
    )
    cell = soup.find("td")
    before = str(cell)

    assert regrades._classify_completion(cell) is None
    assert str(cell) == before


def test_regrade_docstring_describes_the_visible_evidence_rule() -> None:
    doc = _flat(regrades.get_regrade_requests.__doc__)
    assert "positive, visible evidence" in doc
    assert "a hidden or greyed-out check icon, a generic ``check`` class" in doc
    assert "a checkbox whose state contradicts the cell's visible text" in doc
