import html
import json
from pathlib import Path
from types import SimpleNamespace

from gradescope_mcp.tools import assignments, extensions, grading_ops, submissions


def test_upload_submission_requires_absolute_path(tmp_path: Path) -> None:
    relative = tmp_path.name
    result = submissions.upload_submission("1", "2", [relative], confirm_write=True)
    assert "file path must be absolute" in result


def test_upload_submission_requires_confirm(tmp_path: Path) -> None:
    file_path = tmp_path / "submission.txt"
    file_path.write_text("hello", encoding="utf-8")

    result = submissions.upload_submission("1", "2", [str(file_path)])

    assert "Write confirmation required" in result
    assert "No changes were made." in result
    assert "confirm_write=True" in result


class _ReadOnlySession:
    """Serves one GET page; any POST fails the test."""

    def __init__(self, page: str) -> None:
        self.page = page

    def get(self, url, **_kwargs):
        return SimpleNamespace(status_code=200, text=self.page, url=url)

    def post(self, *_args, **_kwargs):
        raise AssertionError("a preview must not write")


def _read_only_conn(page: str) -> SimpleNamespace:
    return SimpleNamespace(session=_ReadOnlySession(page), gradescope_base_url="https://x")


def test_modify_assignment_dates_requires_confirm(monkeypatch) -> None:
    form = (
        '<form><input name="authenticity_token" value="T">'
        '<input name="assignment[release_date_string]" value="2026-03-01T00:00">'
        '<input name="assignment[due_date_string]" value="2026-03-19T12:00">'
        '<input type="checkbox" name="assignment[allow_late_submissions]" value="1">'
        '<input name="assignment[hard_due_date_string]" value=""></form>'
    )
    monkeypatch.setattr(assignments, "get_connection", lambda: _read_only_conn(form))

    result = assignments.modify_assignment_dates(
        "1",
        "2",
        due_date="2026-03-20T12:00",
    )

    assert "Write confirmation required" in result
    assert "modify_assignment_dates" in result
    assert "due_date=2026-03-20T12:00" in result


def test_modify_assignment_dates_preview_fails_without_login() -> None:
    # No credentials: the preview can't list the values that will be sent.
    result = assignments.modify_assignment_dates("1", "2", due_date="2026-03-20T12:00")

    assert result.startswith("Authentication error:")


def test_set_extension_requires_confirm(monkeypatch) -> None:
    props = {
        "override": {"user_id": 9, "settings": {}},
        "timezone": {"identifier": "America/New_York"},
    }
    page = (
        '<table class="table js-overridesTable"><tbody><tr><td>'
        '<div data-react-class="EditExtension" data-react-props="'
        f'{html.escape(json.dumps(props))}"></div></td></tr></tbody></table>'
    )
    monkeypatch.setattr(extensions, "get_connection", lambda: _read_only_conn(page))

    result = extensions.set_extension(
        "1",
        "2",
        "3",
        due_date="2026-03-20T12:00",
    )

    assert "Write confirmation required" in result
    assert "set_extension" in result
    assert "user_id=`3`" in result


def test_set_extension_preview_fails_without_login() -> None:
    result = extensions.set_extension("1", "2", "3", due_date="2026-03-20T12:00")

    assert result.startswith("Authentication error:")


def test_apply_grade_requires_confirm(monkeypatch) -> None:
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "question": {"weight": 10, "scoring_type": "negative"},
                "submission": {"score": 4.0, "graded": True},
                "rubric_items": [
                    {"id": 10, "description": "Sign error", "weight": 2},
                    {"id": 20, "description": "Missing units", "weight": 1},
                    {"id": 30, "description": "Blank", "weight": 10},
                ],
                "rubric_item_evaluations": [{"rubric_item_id": 30, "present": True}],
                "urls": {"save_grade": "/courses/1/questions/2/submissions/3/save_grade"},
            },
            "session": object(),
            "csrf_token": "token",
            "base_url": "https://example.com",
        },
    )

    result = grading_ops.apply_grade(
        "1",
        "2",
        "3",
        rubric_item_ids=["10", "20"],
        point_adjustment=-1.0,
        comment="Needs revision",
    )

    assert "Write confirmation required" in result
    assert "apply_grade" in result
    assert "current_score=4.0" in result
    assert "point_adjustment=-1.0" in result
    # The preview resolves the IDs: what gets checked AND unchecked.
    assert "will CHECK: `10` Sign error (2 pts); `20` Missing units (1 pts)" in result
    assert "will UNCHECK: `30` Blank (10 pts)" in result
    # 10 - 2 - 1 - 1 = 6
    assert "projected_score=6/10" in result


class _RubricPageConn:
    """Fake connection whose question pages expose one submission's grader props.

    Any write request fails the test: previews must only read.
    """

    def __init__(self, props: dict):
        self.gradescope_base_url = "https://example.com"
        self.calls: list = []
        self._grader = (
            '<meta name="csrf-token" content="tok">'
            '<div data-react-class="SubmissionGrader" data-react-props="'
            + html.escape(json.dumps(props), quote=True)
            + '"></div>'
        )
        self._listing = '<a href="/courses/1/questions/2/submissions/77/grade">grade</a>'
        self.session = SimpleNamespace(
            get=self._get,
            post=self._write("POST"),
            put=self._write("PUT"),
            delete=self._write("DELETE"),
        )

    def _get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        body = self._grader if url.endswith("/submissions/77/grade") else self._listing
        return SimpleNamespace(status_code=200, text=body, url=url)

    def _write(self, method):
        def _call(url, **_kwargs):
            self.calls.append((method, url))
            raise AssertionError("preview must not write")

        return _call


def test_create_rubric_item_requires_confirm(monkeypatch) -> None:
    conn = _RubricPageConn(
        {"question": {"weight": 10, "scoring_type": "negative"}, "rubric_items": []}
    )
    monkeypatch.setattr(grading_ops, "get_connection", lambda: conn)

    result = grading_ops.create_rubric_item("1", "2", "Missing proof", 2.0)

    assert "Write confirmation required" in result
    assert "create_rubric_item" in result
    assert "weight=2.0" in result
    assert "will DEDUCT 2 point(s) (negative scoring)" in result
    assert all(method == "GET" for method, _url in conn.calls)


def test_create_rubric_item_rejects_negative_weight_before_any_request(monkeypatch) -> None:
    def fail_get_connection():
        raise AssertionError("must not touch Gradescope")

    monkeypatch.setattr(grading_ops, "get_connection", fail_get_connection)

    result = grading_ops.create_rubric_item("1", "2", "Missing proof", -2.0)

    assert result.startswith("Error: weight must not be negative")
