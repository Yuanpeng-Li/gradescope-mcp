import json
from types import SimpleNamespace

from gradescope_mcp.tools import assignment_admin as admin


def test_settings_preserve_fields_and_bind_snapshot(monkeypatch):
    calls = []
    state = {
        "assignment[title]": "Exam",
        "assignment[template_visible_to_students]": "0",
        "_method": "patch",
    }

    def settings(*a):
        return (
            "<unused>",
            dict(state),
            {"assignment[template_visible_to_students]": ["1"]},
        )

    monkeypatch.setattr(admin, "_settings", settings)
    monkeypatch.setattr(admin, "_token", lambda s: "csrf")

    def post(url, **kw):
        calls.append(kw)
        state.update({k: v for k, v in kw["data"].items() if k != "authenticity_token"})
        return SimpleNamespace(status_code=302)

    monkeypatch.setattr(
        admin,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://example.com",
            session=SimpleNamespace(post=post),
        ),
    )
    p = json.loads(
        admin.prepare_assignment_plan("1", "settings", {"title": "Updated"}, "2")
    )
    assert not calls
    result = json.loads(
        admin.execute_assignment_plan(p["operation_token"], p["request_digest"], True)
    )
    assert (
        result["status"] == "verified"
        and state["assignment[template_visible_to_students]"] == "0"
    )
    assert admin.execute_assignment_plan(
        p["operation_token"], p["request_digest"], True
    ).startswith("Error:")
    assert len(calls) == 1


def test_create_requires_template_and_student_dates(monkeypatch):
    monkeypatch.setattr(admin, "get_connection", lambda: object())
    assert "require template_path" in admin.prepare_assignment_plan(
        "1", "create", {"kind": "exam", "title": "Exam"}
    )


def test_form_checkbox_and_tokens():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<form><input name="authenticity_token" value="SECRET"><input name="x" value="0" type="hidden"><input name="x" value="1" type="checkbox" checked><input name="y" value="bad" disabled></form>',
        "html.parser",
    )
    fields, choices = admin._form(soup.form)
    assert fields == {"x": "1"} and choices == {"x": ["0", "1"]}


def test_bad_form_action_is_refused():
    import pytest

    c = SimpleNamespace(gradescope_base_url="https://example.com")
    with pytest.raises(ValueError):
        admin._bound(
            c,
            "https://attacker.example/courses/1/assignments",
            "/courses/1/assignments",
        )
