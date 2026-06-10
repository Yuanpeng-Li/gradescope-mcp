import importlib.util
import sys
from pathlib import Path


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
