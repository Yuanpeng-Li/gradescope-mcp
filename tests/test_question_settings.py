import copy
import json
import re
from types import SimpleNamespace

import pytest

from gradescope_mcp.tools import question_settings as qs


def revision(preview):
    return re.search(r"expected_revision=([a-f0-9]+)", preview).group(1)


@pytest.fixture
def scoring(monkeypatch):
    state = {
        "id": 2,
        "scoring_type": "negative",
        "floor": True,
        "ceiling": True,
        "weight": "0.0",
    }
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        sent = kwargs["json"]["question"]
        assert "weight" not in sent
        state.update(
            {k: (v == "1") if k in ("floor", "ceiling") else v for k, v in sent.items()}
        )
        return SimpleNamespace(status_code=200)

    ctx = {
        "base_url": "https://example.com",
        "csrf_token": "csrf",
        "session": SimpleNamespace(post=post),
    }

    def load(*args):
        return {
            **ctx,
            "props": {
                "question": dict(state),
                "urls": {
                    "save_grading_options": "/courses/1/questions/2/save_grading_options"
                },
            },
        }

    monkeypatch.setattr(qs, "_load_question_rubric_context", load)
    return state, calls


def test_zero_point_bonus_preserves_weight_and_floor(scoring):
    state, calls = scoring
    preview = qs.set_question_scoring("1", "2", "positive", False)
    assert "No changes" in preview and not calls
    result = qs.set_question_scoring(
        "1",
        "2",
        "positive",
        False,
        expected_revision=revision(preview),
        confirm_write=True,
    )
    assert "verified" in result
    assert state == {
        "id": 2,
        "scoring_type": "positive",
        "floor": True,
        "ceiling": False,
        "weight": "0.0",
    }
    assert calls[0]["json"]["question"]["ceiling"] == "0"
    assert calls[0]["json"]["question"]["floor"] == "1"


def test_scoring_stale_revision_and_missing_confirmation(scoring):
    state, calls = scoring
    p = qs.set_question_scoring("1", "2", "positive", False)
    state["floor"] = False
    assert "changed" in qs.set_question_scoring(
        "1", "2", "positive", False, expected_revision=revision(p), confirm_write=True
    )
    assert "required" in qs.set_question_scoring(
        "1", "2", "positive", False, confirm_write=True
    )
    assert not calls


def test_scoring_readback_weight_change_warns(scoring):
    state, calls = scoring
    p = qs.set_question_scoring("1", "2", "positive", False)
    original = qs._scoring

    def read(*a):
        if calls:
            state["weight"] = "5.0"
        return original(*a)

    from unittest.mock import patch

    with patch.object(qs, "_scoring", read):
        assert "read-back differs" in qs.set_question_scoring(
            "1",
            "2",
            "positive",
            False,
            expected_revision=revision(p),
            confirm_write=True,
        )


@pytest.fixture
def grouping(monkeypatch):
    pages = {
        str(i): {
            "question": {
                "id": i,
                "course_id": 1,
                "assignment_id": 7,
                "assisted_grading_type": "not_grouped",
            },
            "groups": [],
            "submissions": [
                {
                    "id": 10,
                    "graded": False,
                    "graded_individually": False,
                    "confirmed_group_id": None,
                    "unconfirmed_group_id": None,
                }
            ],
            "urls": {
                "question_url": f"/courses/1/questions/{i}",
                "answer_groups_url": f"/courses/1/questions/{i}/answer_groups",
                "confirmed_memberships_url": f"/courses/1/questions/{i}/answer_group_memberships/many",
            },
        }
        for i in (2, 3)
    }
    calls = []

    def patch(url, **kwargs):
        calls.append(("patch", url, kwargs))
        pages[url.rsplit("/", 1)[1]]["question"].update(kwargs["json"]["question"])
        return SimpleNamespace(status_code=200)

    def post(url, **kwargs):
        calls.append(("post", url, kwargs))
        if url.endswith("/answer_groups"):
            pages["2"]["groups"].append({"id": 99, "title": kwargs["json"]["title"]})
            return SimpleNamespace(status_code=201, json=lambda: {"id": 99})
        pages["2"]["submissions"][0]["confirmed_group_id"] = 99
        return SimpleNamespace(status_code=200)

    conn = SimpleNamespace(
        gradescope_base_url="https://example.com",
        session=SimpleNamespace(patch=patch, post=post),
    )
    monkeypatch.setattr(
        qs, "_grouper", lambda c, q: (conn, copy.deepcopy(pages[str(q)]), "csrf")
    )
    monkeypatch.setattr(
        qs, "_fetch_assignment_questions", lambda *a: {"2": {}, "3": {}}
    )
    return pages, calls, conn


def test_method_all_preview_and_verify(grouping):
    pages, calls, _ = grouping
    p = qs.set_question_grading_method("1", "2", "groups", True)
    assert not calls and '"3"' in p
    assert "verified" in qs.set_question_grading_method(
        "1", "2", "groups", True, revision(p), True
    )
    assert len(calls) == 2
    assert all(
        v["question"]["assisted_grading_type"] == "complex" for v in pages.values()
    )


def test_ai_regions_required(grouping):
    assert "requires configured answer regions" in qs.set_question_grading_method(
        "1", "2", "multiple_choice"
    )
    assert not grouping[1]


def test_manual_group_create_and_membership(grouping):
    pages, calls, _ = grouping
    pages["2"]["question"]["assisted_grading_type"] = "complex"
    p = qs.create_answer_group("1", "2", "C", ["10"])
    assert not calls and "No changes" in p
    assert "confirmed members verified" in qs.create_answer_group(
        "1", "2", "C", ["10"], revision(p), True
    )
    assert len(calls) == 2
    assert calls[1][2]["json"] == {"answer_group_id": 99, "submission_ids": [10]}


def test_group_refuses_graded_or_foreign_members(grouping):
    pages, calls, _ = grouping
    pages["2"]["question"]["assisted_grading_type"] = "complex"
    assert "do not belong" in qs.create_answer_group("1", "2", "C", ["88"])
    pages["2"]["submissions"][0]["graded"] = True
    assert "Only explicitly ungraded" in qs.create_answer_group("1", "2", "C", ["10"])
    assert not calls


def test_partial_group_reports_created_id(grouping):
    pages, calls, conn = grouping
    pages["2"]["question"]["assisted_grading_type"] = "complex"
    p = qs.create_answer_group("1", "2", "C", ["10"])
    original = conn.session.post

    def post(url, **kwargs):
        if url.endswith("/many"):
            raise OSError("connection lost")
        return original(url, **kwargs)

    conn.session.post = post
    result = qs.create_answer_group("1", "2", "C", ["10"], revision(p), True)
    assert "Created group ID: 99" in result and "partial" in result


def test_scoring_url_rejects_other_question(scoring, monkeypatch):
    original = qs._load_question_rubric_context

    def load(*a):
        ctx = original(*a)
        ctx["props"]["urls"]["save_grading_options"] = (
            "/courses/1/questions/9/save_grading_options"
        )
        return ctx

    monkeypatch.setattr(qs, "_load_question_rubric_context", load)
    assert "Unexpected write URL" in qs.set_question_scoring(
        "1", "2", "positive", False
    )
    assert not scoring[1]
