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


def test_upload_submission_for_student_requires_confirm(tmp_path: Path) -> None:
    file_path = tmp_path / "submission.pdf"
    file_path.write_bytes(b"pdf")

    result = submissions.upload_submission_for_student(
        "1",
        "2",
        "3",
        [str(file_path)],
        submission_id="99",
    )

    assert "Write confirmation required" in result
    assert "upload_submission_for_student" in result
    assert "user_id=`3`" in result
    assert "submission_id=`99`" in result
    assert "confirm_write=True" in result


def test_inspect_submission_upload_form_lists_candidate_fields(monkeypatch) -> None:
    html = """
    <html><body>
      <form method="post" action="/courses/1/assignments/2/submissions">
        <input type="hidden" name="authenticity_token" value="token">
        <select name="submission[user_id]">
          <option value="3">Student A</option>
        </select>
        <input type="file" name="submission[files][]">
        <button>Upload Submission</button>
      </form>
    </body></html>
    """

    session = SimpleNamespace(
        get=lambda _url: SimpleNamespace(status_code=200, text=html)
    )
    monkeypatch.setattr(
        submissions,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://gradescope.test",
            session=session,
        ),
    )

    result = submissions.inspect_submission_upload_form("1", "2")

    assert "File-upload forms found:** 1" in result
    assert "`submission[user_id]`" in result
    assert "`submission[files][]`" in result


def test_upload_submission_for_student_posts_discovered_form(
    tmp_path: Path, monkeypatch
) -> None:
    file_path = tmp_path / "submission.pdf"
    file_path.write_bytes(b"pdf")
    html = """
    <html><body>
      <form method="post" action="/courses/1/assignments/2/submissions">
        <input type="hidden" name="authenticity_token" value="token">
        <select name="submission[user_id]">
          <option value="">Pick one</option>
          <option value="3">Student A</option>
        </select>
        <input type="file" name="submission[files][]">
        <button>Upload Submission</button>
      </form>
    </body></html>
    """

    captured: dict = {}

    class FakeSession:
        def get(self, url):
            captured["get_url"] = url
            return SimpleNamespace(status_code=200, text=html)

        def post(self, url, data, headers):
            captured["post_url"] = url
            captured["headers"] = headers
            captured["field_names"] = [field[0] for field in data.fields]
            captured["student_field_values"] = [
                field[1]
                for field in data.fields
                if field[0] == "submission[user_id]"
            ]
            return SimpleNamespace(
                status_code=200,
                url="https://gradescope.test/courses/1/assignments/2/submissions/42",
            )

    monkeypatch.setattr(
        submissions,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://gradescope.test",
            session=FakeSession(),
        ),
    )

    result = submissions.upload_submission_for_student(
        "1",
        "2",
        "3",
        [str(file_path)],
        confirm_write=True,
    )

    assert "Staff upload request completed" in result
    assert captured["get_url"] == "https://gradescope.test/courses/1/assignments/2/submissions"
    assert captured["post_url"] == "https://gradescope.test/courses/1/assignments/2/submissions"
    assert "Content-Type" in captured["headers"]
    assert "submission[files][]" in captured["field_names"]
    assert captured["student_field_values"] == ["3"]


def test_modify_assignment_dates_requires_confirm() -> None:
    result = assignments.modify_assignment_dates(
        "1",
        "2",
        due_date="2026-03-20T12:00",
    )

    assert "Write confirmation required" in result
    assert "modify_assignment_dates" in result
    assert "due_date=2026-03-20T12:00" in result


def test_set_extension_requires_confirm() -> None:
    result = extensions.set_extension(
        "1",
        "2",
        "3",
        due_date="2026-03-20T12:00",
    )

    assert "Write confirmation required" in result
    assert "set_extension" in result
    assert "user_id=`3`" in result


def test_apply_grade_requires_confirm(monkeypatch) -> None:
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "submission": {"score": 4.0},
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


def test_create_rubric_item_requires_confirm() -> None:
    result = grading_ops.create_rubric_item("1", "2", "Missing proof", -2.0)

    assert "Write confirmation required" in result
    assert "create_rubric_item" in result
    assert "weight=-2.0" in result
