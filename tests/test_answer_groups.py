"""Tests for answer_groups tools."""

import json
from types import SimpleNamespace

from gradescope_mcp.tools import answer_groups


def test_get_answer_groups_requires_ids() -> None:
    result = answer_groups.get_answer_groups("", "123")
    assert "Error" in result

    result = answer_groups.get_answer_groups("123", "")
    assert "Error" in result


def test_get_answer_group_detail_requires_ids() -> None:
    result = answer_groups.get_answer_group_detail("", "123", "456")
    assert "Error" in result


def test_get_answer_groups_json_flags_manual_grouping(monkeypatch) -> None:
    monkeypatch.setattr(
        answer_groups,
        "_fetch_answer_groups_json",
        lambda *_args, **_kwargs: {
            "groups": [],
            "submissions": [{"id": 1, "graded": False}, {"id": 2, "graded": False}],
            "question": {
                "numbered_title": "Q4b",
                "assisted_grading_type": "not_grouped",
            },
            "status": "ready",
        },
    )

    result = json.loads(answer_groups.get_answer_groups("1", "2", output_format="json"))

    assert result["grouping_available"] is False
    assert result["manual_grouping_recommended"] is True
    assert result["recommended_strategy"] == "manual_sampling"


def test_grade_answer_group_requires_confirm(monkeypatch) -> None:
    """grade_answer_group should refuse without confirm_write."""
    monkeypatch.setattr(
        answer_groups,
        "_fetch_answer_groups_json",
        lambda *_args, **_kwargs: {
            "groups": [{"id": 3, "title": "test group"}],
            "submissions": [
                {"id": 101, "confirmed_group_id": 3, "graded": False},
                {"id": 102, "confirmed_group_id": 3, "graded": True},
            ],
        },
    )
    # Mock get_connection for the group grade page request

    class _FakeResp:
        status_code = 200
        text = '<html><meta name="csrf-token" content="tok"><div data-react-class="SubmissionGrader" data-react-props=\'{"urls":{"save_grade":"/courses/1/questions/2/submissions/100/save_grade"},"rubric_items":[{"id":10}],"rubric_item_evaluations":[],"evaluation":{}}\'></div></html>'

    class _FakeSession:
        def get(self, url, **kw):
            return _FakeResp()

    class _FakeConn:
        gradescope_base_url = "https://example.com"
        session = _FakeSession()

    monkeypatch.setattr(answer_groups, "get_connection", lambda: _FakeConn())

    # One member is already graded, so overwriting must be opted into before
    # a preview is even shown.
    refused = answer_groups.grade_answer_group(
        "1", "2", "3",
        rubric_item_ids=["10"],
        comment="test",
    )
    assert refused.startswith("Error:")
    assert "overwrite_graded=True" in refused

    result = answer_groups.grade_answer_group(
        "1", "2", "3",
        rubric_item_ids=["10"],
        comment="test",
        overwrite_graded=True,
    )
    assert "Write confirmation required" in result
    assert "grade_answer_group" in result
    assert "group_size=2 confirmed submissions (1 already graded" in result
    assert "`102`" in result
    assert "confirm_write=True" in result


def test_get_answer_group_detail_json_separates_confirmed_and_inferred(monkeypatch) -> None:
    monkeypatch.setattr(
        answer_groups,
        "_fetch_answer_groups_json",
        lambda *_args, **_kwargs: {
            "groups": [{"id": 3, "title": "cluster"}],
            "submissions": [
                {"id": 101, "confirmed_group_id": 3, "graded": False},
                {"id": 102, "unconfirmed_group_id": 3, "graded": False},
            ],
        },
    )

    result = json.loads(
        answer_groups.get_answer_group_detail("1", "2", "3", output_format="json")
    )

    assert result["size"] == 1
    assert [sub["submission_id"] for sub in result["submissions"]] == ["101"]
    assert [sub["submission_id"] for sub in result["inferred_submissions"]] == ["102"]
    assert "inferred_warning" in result


def test_grade_answer_group_omits_existing_eval_when_not_explicitly_provided(
    monkeypatch,
) -> None:
    captured = {}

    monkeypatch.setattr(
        answer_groups,
        "_fetch_answer_groups_json",
        lambda *_args, **_kwargs: {
            "groups": [{"id": 3, "title": "cluster"}],
            "submissions": [
                {"id": 101, "confirmed_group_id": 3, "graded": False},
                {"id": 102, "confirmed_group_id": 3, "graded": False},
            ],
        },
    )

    class _FakeSession:
        def get(self, _url, **_kw):
            return SimpleNamespace(
                status_code=200,
                text=(
                    '<html><meta name="csrf-token" content="tok">'
                    '<div data-react-class="SubmissionGrader" '
                    'data-react-props=\'{"urls":{"save_grade":"/courses/1/questions/2/submissions/100/save_grade"},'
                    '"rubric_items":[{"id":10}],"rubric_item_evaluations":[],'
                    '"evaluation":{"points":1.5,"comments":"existing one-off comment"}}\'></div></html>'
                ),
            )

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True}, text='{"ok": true}')

    monkeypatch.setattr(
        answer_groups,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://example.com",
            session=_FakeSession(),
        ),
    )

    answer_groups.grade_answer_group(
        "1",
        "2",
        "3",
        rubric_item_ids=["10"],
        confirm_write=True,
    )

    payload = captured["kwargs"]["json"]["question_submission_evaluation"]
    assert payload == {}
    assert captured["url"] == (
        "https://example.com/courses/1/questions/2/submissions/100/save_many_grades"
    )
    assert captured["kwargs"]["allow_redirects"] is False


def test_grade_answer_group_rejects_none_rubric_ids() -> None:
    result = answer_groups.grade_answer_group(
        "1", "2", "3",
        rubric_item_ids=None,
        comment="test",
    )
    assert "Error" in result
    assert "explicitly specified" in result
