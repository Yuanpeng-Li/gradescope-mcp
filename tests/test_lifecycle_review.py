import copy
import json
import re
from types import SimpleNamespace

import pytest

from gradescope_mcp.tools import submission_files as files, review, post_grading as post


def test_download_host_allowlist():
    assert files._trusted(
        "https://production-gradescope-uploads.s3-us-west-2.amazonaws.com/file.pdf",
        "https://www.gradescope.com",
    )
    for url in [
        "http://production-gradescope-uploads.s3.amazonaws.com/a",
        "https://127.0.0.1/a",
        "https://production-gradescope-uploads.s3.amazonaws.com.evil.example/a",
        "https://www.gradescope.com:8443/a",
        "https://user:pass@www.gradescope.com/a",
    ]:
        assert not files._trusted(url, "https://www.gradescope.com")


def test_foreign_submission_cannot_download(monkeypatch):
    monkeypatch.setattr(
        files, "_fetch_assignment_scores_csv", lambda *a: ([{"Submission ID": "1"}], [])
    )
    monkeypatch.setattr(
        files, "get_connection", lambda: pytest.fail("Must validate before connection")
    )
    assert "belong" in files.download_submission_files("1", "2", ["9"])


def test_class_bonus_ties_and_complete_population(monkeypatch):
    monkeypatch.setattr(
        review,
        "_fetch_question_submission_entries",
        lambda *a: [{"submission_id": str(i)} for i in (1, 2, 3)],
    )
    monkeypatch.setattr(
        review,
        "_load_question_rubric_context",
        lambda *a: {
            "props": {
                "question": {
                    "weight": "0.0",
                    "scoring_type": "positive",
                    "ceiling": False,
                }
            }
        },
    )
    captured = {}

    def prepare(c, q, grades):
        captured["grades"] = grades
        return json.dumps({"operation_token": "example"})

    monkeypatch.setattr(review, "prepare_grade_plan", prepare)
    assert "exactly all" in review.prepare_choice_bonus_plan("1", "2", {"1": "A"})
    result = json.loads(
        review.prepare_choice_bonus_plan("1", "2", {"1": "A", "2": "B", "3": "C"})
    )
    assert result["choice_scores"] == {"A": 2, "B": 4, "C": 1}
    assert [r["point_adjustment"] for r in captured["grades"]] == [2, 4, 1]


def test_regrade_reply_bound_to_current_request(monkeypatch):
    props = {
        "submission": {"id": 3},
        "open_request": {"id": 4, "staff_comment": "", "completed": False},
        "closed_requests": [],
        "urls": {
            "regrade_request": "/courses/1/questions/2/submissions/3/regrade_requests"
        },
    }
    calls = []

    def put(url, **kw):
        calls.append(kw)
        props["open_request"].update(kw["json"]["regrade_request"])
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        post,
        "_get_grading_context",
        lambda *a: {
            "props": copy.deepcopy(props),
            "base_url": "https://example.com",
            "csrf_token": "csrf",
            "session": SimpleNamespace(put=put),
        },
    )
    preview = post.respond_regrade_request("1", "2", "3", "4", "Reviewed")
    revision = re.search("expected_revision=([a-f0-9]+)", preview)[1]
    assert not calls
    assert "verified" in post.respond_regrade_request(
        "1", "2", "3", "4", "Reviewed", True, revision, True
    )
    assert calls[0]["json"]["regrade_request"]["completed"] is True
    assert "does not belong" in post.respond_regrade_request(
        "1", "2", "3", "9", "Reviewed"
    )


def test_extension_readback_error_not_success(monkeypatch):
    calls = []
    current = {
        "override": {"id": 5, "user_id": 3, "settings": {}},
        "deletePath": "/courses/1/assignments/2/extensions/5",
    }

    def read(*a):
        if calls:
            raise ValueError("Forbidden page")
        return copy.deepcopy(current), "csrf"

    monkeypatch.setattr(post, "_extension", read)
    monkeypatch.setattr(
        post,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://example.com",
            session=SimpleNamespace(
                delete=lambda *a, **k: (
                    calls.append(1) or SimpleNamespace(status_code=200)
                )
            ),
        ),
    )
    preview = post.modify_extension_options("1", "2", "3", "delete")
    revision = re.search("expected_revision=([a-f0-9]+)", preview)[1]
    assert "could not verify" in post.modify_extension_options(
        "1", "2", "3", "delete", expected_revision=revision, confirm_write=True
    )
