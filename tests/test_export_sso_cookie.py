import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from gradescope_mcp import auth, envfiles


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_sso_cookie.py"
SPEC = importlib.util.spec_from_file_location("export_sso_cookie", SCRIPT_PATH)
assert SPEC is not None
export_sso_cookie = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(export_sso_cookie)


def _parse_args(monkeypatch, args: list[str]):
    monkeypatch.setattr(sys, "argv", ["export_sso_cookie.py", *args])
    return export_sso_cookie.parse_args()


def test_sso_cookie_export_defaults_to_manual_confirmation(monkeypatch) -> None:
    args = _parse_args(monkeypatch, [])

    assert args.manual_confirm is True


def test_sso_cookie_export_accepts_explicit_manual_confirmation(monkeypatch) -> None:
    args = _parse_args(monkeypatch, ["--manual-confirm"])

    assert args.manual_confirm is True


def test_sso_cookie_export_auto_detect_is_opt_in(monkeypatch) -> None:
    args = _parse_args(monkeypatch, ["--auto-detect"])

    assert args.manual_confirm is False


HEADER = '_gradescope_session=abc%3D%3D--ff; signed_token=a"b\\c'


def test_write_env_round_trips_through_dotenv(tmp_path) -> None:
    env = tmp_path / ".env"

    export_sso_cookie.write_env(env, HEADER)

    value = dotenv_values(env)["GRADESCOPE_COOKIE_HEADER"]
    assert value == HEADER
    assert auth.parse_cookie_header(value)["_gradescope_session"] == "abc%3D%3D--ff"


def test_write_env_replaces_old_cookies_and_keeps_other_settings(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "GRADESCOPE_EMAIL=prof@example.edu\n"
        "export GRADESCOPE_COOKIE_HEADER=old-header\n"
        "  GRADESCOPE_SESSION_COOKIE = old-session\n"
        "GRADESCOPE_MCP_HTTP_TIMEOUT=30\n\n"
    )

    export_sso_cookie.write_env(env, HEADER)

    values = dotenv_values(env)
    assert values == {
        "GRADESCOPE_EMAIL": "prof@example.edu",
        "GRADESCOPE_MCP_HTTP_TIMEOUT": "30",
        "GRADESCOPE_COOKIE_HEADER": HEADER,
    }
    assert "old-" not in env.read_text()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permissions")
def test_write_env_leaves_a_private_file_the_server_will_load(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("GRADESCOPE_EMAIL=prof@example.edu\n")
    env.chmod(0o666)

    export_sso_cookie.write_env(env, HEADER)

    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert envfiles._untrusted_reason(env) is None
    assert [p.name for p in tmp_path.iterdir()] == [".env"]  # no temp file left
