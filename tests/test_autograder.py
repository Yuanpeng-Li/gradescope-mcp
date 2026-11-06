import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from gradescope_mcp.tools import autograder as ag


def test_config_plan_binds_form_and_refuses_replay(monkeypatch):
    page = BeautifulSoup(
        '<meta name="csrf-token" content="csrf"><form class="js-autograderForm" action="/courses/1/assignments/2/configure_autograder" method="post"><input name="assignment[image_name]" value="old"></form>',
        "html.parser",
    )
    sent = []
    conn = SimpleNamespace(
        gradescope_base_url="https://example.com",
        session=SimpleNamespace(
            post=lambda *a, **k: sent.append(k) or SimpleNamespace(status_code=302)
        ),
    )
    monkeypatch.setattr(ag, "get_connection", lambda: conn)
    monkeypatch.setattr(ag.admin, "_page", lambda *a: page)
    p = json.loads(
        ag.prepare_autograder_plan(
            "1", "2", "configure", {"assignment[image_name]": "new"}
        )
    )
    assert not sent
    result = json.loads(
        ag.execute_autograder_plan(p["operation_token"], p["request_digest"], True)
    )
    assert result["status"] == "accepted_unverified"
    assert ag.execute_autograder_plan(
        p["operation_token"], p["request_digest"], True
    ).startswith("Error:")
    assert len(sent) == 1


def test_rerun_unavailable_fails_closed(monkeypatch):
    monkeypatch.setattr(ag, "get_connection", lambda: object())
    monkeypatch.setattr(
        ag.admin,
        "_page",
        lambda *a: BeautifulSoup("<html>No rerun form</html>", "html.parser"),
    )
    assert "No unambiguous" in ag.prepare_autograder_plan("1", "2", "rerun", {})


def test_zip_validation_never_extracts(tmp_path):
    path = tmp_path / "autograder.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("setup.sh", "echo ok")
        z.writestr("run_autograder", "echo ok")
    assert ag._zip(str(path))["size"] > 0
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("../escape", "bad")
    with pytest.raises(ValueError, match="Unsafe"):
        ag._zip(str(path))
