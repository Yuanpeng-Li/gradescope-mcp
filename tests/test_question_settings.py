"""Tests for tool_set_question_scoring (a question's scoring type and bounds)."""

import html
import json
from types import SimpleNamespace

import pytest

from gradescope_mcp.tools import question_settings

BASE = "https://gs"
C, A, Q = "1", "8", "20"


class FakeGradescope:
    """One assignment with a 0-point bonus question and a deduction question."""

    def __init__(self, monkeypatch, *, link=True, has_submissions=False, apply_save=True):
        self.questions = [
            {"id": 20, "title": "Bonus", "weight": "0.0", "scoring_type": "negative",
             "floor": True, "ceiling": True},
            {"id": 21, "title": "Other", "weight": "2.0", "scoring_type": "negative",
             "floor": True, "ceiling": True},
        ]
        self.rubric_items = [
            {"id": 1, "question_id": 20, "description": "Correct", "weight": "0.0"},
            {"id": 2, "question_id": 20, "description": "Explains why", "weight": "5.0"},
            {"id": 3, "question_id": 21, "description": "Incorrect", "weight": "2.0"},
        ]
        self.link = link
        self.has_submissions = has_submissions
        self.apply_save = apply_save
        self.posts = []
        session = SimpleNamespace(get=self.get, post=self.post)
        conn = SimpleNamespace(gradescope_base_url=BASE, session=session)
        monkeypatch.setattr(question_settings, "get_connection", lambda: conn)

    def get(self, url, **_kwargs):
        if url == f"{BASE}/courses/{C}/questions/{Q}/submissions":
            sidebar = f'<a href="/courses/{C}/assignments/{A}/rubric/edit">Create Rubric</a>' if self.link else ""
            return SimpleNamespace(status_code=200, text=sidebar)
        if url == f"{BASE}/courses/{C}/assignments/{A}/rubric/edit":
            props = {
                "questions": self.questions,
                "rubric_items": self.rubric_items,
                "has_submissions_and_students": self.has_submissions,
                "urls": {"question_urls": {
                    str(q["id"]): {"save_grading_options": f"/courses/{C}/questions/{q['id']}/save_grading_options"}
                    for q in self.questions
                }},
            }
            text = (
                '<meta name="csrf-token" content="TOKEN">'
                '<div data-react-class="AssignmentRubric" data-react-props="'
                + html.escape(json.dumps(props), quote=True) + '"></div>'
            )
            return SimpleNamespace(status_code=200, text=text)
        return SimpleNamespace(status_code=404, text="")

    def post(self, url, json, headers):
        self.posts.append({"url": url, "json": json, "headers": headers})
        if self.apply_save:
            sent = json["question"]
            q = next(q for q in self.questions if q["id"] == sent["id"])
            q.update(scoring_type=sent["scoring_type"], ceiling=sent["ceiling"] == "1",
                     floor=sent["floor"] == "1")
        return SimpleNamespace(status_code=200, text="{}")


def test_preview_shows_changes_and_flipped_items_without_sending(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)

    result = question_settings.set_question_scoring(C, Q, "positive", ceiling=False)

    assert gs.posts == []
    assert "No changes were made." in result
    assert "scoring_type: negative → positive" in result
    assert "ceiling: on → off" in result
    assert "floor: on (unchanged)" in result
    assert "1 existing rubric item(s) will flip direction (deduct → add): Explains why (5.0)" in result
    assert "may exceed its points (bonus)" in result


def test_confirm_sends_the_editor_request_and_reads_back(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)

    result = question_settings.set_question_scoring(
        C, Q, "positive", ceiling=False, confirm_write=True
    )

    assert result.startswith("✅ Scoring settings saved"), result
    assert "**Now:** scoring positive, ceiling off, floor on" in result
    assert "Read-back" not in result
    (call,) = gs.posts
    assert call["url"] == f"{BASE}/courses/{C}/questions/{Q}/save_grading_options"
    assert call["headers"]["X-CSRF-Token"] == "TOKEN"
    assert call["json"] == {
        "question": {"id": 20, "scoring_type": "positive", "ceiling": "0", "floor": "1"}
    }
    assert gs.questions[1]["scoring_type"] == "negative"  # other questions untouched


def test_unchanged_settings_send_nothing(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)

    result = question_settings.set_question_scoring(
        C, Q, "negative", ceiling=True, confirm_write=True
    )

    assert "nothing was sent" in result
    assert gs.posts == []


def test_read_back_mismatch_is_reported(monkeypatch) -> None:
    FakeGradescope(monkeypatch, apply_save=False)

    result = question_settings.set_question_scoring(C, Q, ceiling=False, confirm_write=True)

    assert "⚠️ Read-back: ceiling is on on Gradescope (sent off)" in result


def test_submissions_warning(monkeypatch) -> None:
    FakeGradescope(monkeypatch, has_submissions=True)

    result = question_settings.set_question_scoring(C, Q, "positive")

    assert "graded scores on this question will be recomputed" in result


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "at least one of"),
        ({"scoring_type": "both"}, 'must be "positive" or "negative"'),
        ({"ceiling": "false"}, "ceiling must be true, false or null"),
    ],
)
def test_invalid_arguments_are_refused_before_loading(monkeypatch, kwargs, message) -> None:
    monkeypatch.setattr(
        question_settings, "get_connection", lambda: pytest.fail("Gradescope contacted")
    )

    result = question_settings.set_question_scoring(C, Q, **kwargs)

    assert result.startswith("Error:") and message in result


def test_missing_rubric_editor_link_is_an_error(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch, link=False)

    result = question_settings.set_question_scoring(C, Q, "positive", confirm_write=True)

    assert result.startswith("Error: no rubric editor link")
    assert gs.posts == []
