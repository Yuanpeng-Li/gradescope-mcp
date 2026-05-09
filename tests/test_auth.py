"""Tests for the auth module's session-recovery helpers.

The earlier ``auth`` module advertised "automatic re-login support" but
``GSConnection.logged_in`` is a one-shot flag that never resets, so the cached
connection looked valid forever even after Gradescope's server-side cookie had
expired. These tests pin the helpers (``is_session_expired_response``,
``with_session_retry``, the upstream-aware ``reset_connection``) that
make the recovery contract real and explicit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from gradescope_mcp import auth


def test_is_session_expired_response_detects_401_and_login_redirect() -> None:
    expired_401 = SimpleNamespace(status_code=401, headers={})
    expired_redirect = SimpleNamespace(status_code=302, headers={"Location": "/login"})
    expired_acct_auth = SimpleNamespace(status_code=302, headers={"Location": "/account/auth"})
    ok = SimpleNamespace(status_code=200, headers={})
    other_redirect = SimpleNamespace(status_code=302, headers={"Location": "/courses/1"})

    assert auth.is_session_expired_response(expired_401) is True
    assert auth.is_session_expired_response(expired_redirect) is True
    assert auth.is_session_expired_response(expired_acct_auth) is True
    assert auth.is_session_expired_response(ok) is False
    assert auth.is_session_expired_response(other_redirect) is False


def test_with_session_retry_resets_and_retries_on_401(monkeypatch) -> None:
    """Expired-session response triggers reset_connection + one retry."""
    calls = {"count": 0, "reset_called": 0}

    fake_conn_v1 = SimpleNamespace(logged_in=True, name="v1")
    fake_conn_v2 = SimpleNamespace(logged_in=True, name="v2")
    connections = iter([fake_conn_v1, fake_conn_v2])

    def fake_get_connection():
        return next(connections)

    def fake_reset():
        calls["reset_called"] += 1

    monkeypatch.setattr(auth, "get_connection", fake_get_connection)
    monkeypatch.setattr(auth, "reset_connection", fake_reset)

    def call(conn):
        calls["count"] += 1
        if calls["count"] == 1:
            assert conn.name == "v1"
            return SimpleNamespace(status_code=401, headers={})
        assert conn.name == "v2"
        return SimpleNamespace(status_code=200, headers={}, body="ok")

    result = auth.with_session_retry(call)

    assert calls["count"] == 2
    assert calls["reset_called"] == 1
    assert result.status_code == 200


def test_with_session_retry_propagates_non_auth_failures(monkeypatch) -> None:
    """A non-session HTTPError must not be silently retried."""
    monkeypatch.setattr(auth, "get_connection", lambda: SimpleNamespace(logged_in=True))
    reset_called = {"n": 0}
    monkeypatch.setattr(
        auth, "reset_connection",
        lambda: reset_called.__setitem__("n", reset_called["n"] + 1),
    )

    def call(conn):
        bad = SimpleNamespace(status_code=500, headers={})
        err = requests.HTTPError("boom")
        err.response = bad
        raise err

    with pytest.raises(requests.HTTPError):
        auth.with_session_retry(call)
    assert reset_called["n"] == 0


def test_reset_connection_invokes_logout_when_available() -> None:
    """If the cached connection exposes logout(), reset calls it best-effort."""
    logout_called = {"n": 0}

    class Conn:
        logged_in = True
        def logout(self):
            logout_called["n"] += 1

    auth._connection = Conn()
    auth.reset_connection()
    assert logout_called["n"] == 1
    assert auth._connection is None


def test_reset_connection_swallows_logout_failures() -> None:
    """A failing upstream logout() must not stop us from clearing local state."""
    class Conn:
        logged_in = True
        def logout(self):
            raise RuntimeError("upstream broke")

    auth._connection = Conn()
    auth.reset_connection()
    assert auth._connection is None
