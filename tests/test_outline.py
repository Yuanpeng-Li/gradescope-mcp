"""Tests for tool_update_assignment_outline (scanned-PDF outline writes)."""

import copy

import pytest

from gradescope_mcp.tools import outline


def _rect(page, y1, y2, x1=0, x2=100):
    return {"x1": x1, "x2": x2, "y1": y1, "y2": y2, "page_number": page}


PROPS = {
    "assignment": {
        "id": 2,
        "title": "Midterm exam",
        "id_regions": {"name": _rect(1, 37.7, 40.9, 17.1, 88.2), "sid": _rect(1, 44.3, 47.6, 16.4, 88.3)},
    },
    "template": {"page_count": 7},
    "has_submissions_and_students": False,
    "outline": [
        {"id": 11, "title": "", "weight": "1.0", "crop_rect_list": [_rect(1, 54, 58.5)]},
        {
            "id": 12, "title": "", "weight": "2.0", "crop_rect_list": [_rect(2, 55.6, 66.8)],
            "children": [
                {"id": 13, "title": "", "weight": "1.0", "crop_rect_list": [_rect(2, 51.9, 66.5)]},
                {"id": 14, "title": "", "weight": "1.0", "crop_rect_list": [_rect(2, 67.3, 99.9)]},
            ],
        },
    ],
}


class FakeSession:
    def __init__(self, gs):
        self.gs = gs

    def patch(self, url, json, headers):
        self.gs.sent.append({"url": url, "json": json, "headers": headers})
        saved = []
        next_id = [100]

        def to_props(q):
            if "id" in q:
                qid = q["id"]
            else:
                next_id[0] += 1
                qid = next_id[0]
            out = {"id": int(qid), "title": q["title"], "weight": str(q["weight"]),
                   "crop_rect_list": q["crop_rect_list"]}
            if q.get("children"):
                out["children"] = [to_props(c) for c in q["children"]]
            return out

        saved = [to_props(q) for q in json["question_data"]]
        self.gs.props["outline"] = saved
        return type("Resp", (), {"status_code": self.gs.status, "text": "{}"})()


class FakeGradescope:
    def __init__(self, monkeypatch, props=None, status=200):
        self.props = copy.deepcopy(props or PROPS)
        self.sent = []
        self.status = status
        conn = type("Conn", (), {"gradescope_base_url": "https://gs", "session": FakeSession(self)})()
        monkeypatch.setattr(
            outline, "_load_outline_editor",
            lambda c, a: {"props": copy.deepcopy(self.props), "csrf_token": "tok", "conn": conn},
        )


FULL = [
    {"question_id": "11", "title": "T/F 1", "weight": 2, "crop_rect_list": [_rect(1, 54, 58.5)]},
    {
        "question_id": "12", "title": "Scenario",
        "children": [
            {"question_id": "13", "title": "Story", "weight": 5, "crop_rect_list": [_rect(2, 51.9, 66.5)]},
            {"question_id": "14", "title": "Trade-off", "weight": 7, "crop_rect_list": [_rect(2, 67.3, 99.9)]},
        ],
    },
    {"title": "Velocity", "weight": 10, "crop_rect_list": [_rect(2, 7.84, 36.1)]},
]


def test_preview_sends_nothing_and_shows_changes_and_fingerprint(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)

    result = outline.update_assignment_outline("1", "2", FULL)

    assert gs.sent == []
    assert "No changes were made." in result
    assert f"`{outline.outline_fingerprint(PROPS)}`" in result
    assert "total points=24" in result
    assert "| 3 | (new) | Velocity | 10 | p2 y 7.8–36.1 | new |" in result
    assert "| 2.1 | `13` | Story | 5 | p2 y 51.9–66.5 | changed: title, points |" in result
    # The group defaults to the sum of its children and the first child's region.
    assert "| 2 | `12` | Scenario | 12 | p2 y 51.9–66.5 | changed: title, points, regions |" in result


def test_confirm_sends_the_editor_payload_and_reads_back(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)
    fp = outline.outline_fingerprint(PROPS)

    result = outline.update_assignment_outline("1", "2", FULL, fp, confirm_write=True)

    assert result.startswith("✅ Outline saved"), result
    assert "Read-back" not in result
    (call,) = gs.sent
    assert call["url"] == "https://gs/courses/1/assignments/2/outline/"
    assert call["headers"]["X-CSRF-Token"] == "tok"
    body = call["json"]
    assert body["assignment"]["identification_regions"]["name"] == _rect(1, 37.7, 40.9, 17.1, 88.2)
    data = body["question_data"]
    assert [q.get("id") for q in data] == ["11", "12", None]
    assert "id" not in data[2]
    assert data[2]["crop_rect_list"] == [_rect(2, 7.8, 36.1)]  # rounded to 0.1 like the editor
    assert data[1]["weight"] == 12
    assert [c["id"] for c in data[1]["children"]] == ["13", "14"]


def test_confirm_requires_a_matching_fingerprint(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)

    missing = outline.update_assignment_outline("1", "2", FULL, confirm_write=True)
    stale = outline.update_assignment_outline("1", "2", FULL, "000000000000", confirm_write=True)

    assert missing.startswith("Error:") and "expected_fingerprint" in missing
    assert stale.startswith("Error:") and "changed since the preview" in stale
    assert gs.sent == []


def test_leaving_out_a_question_is_refused_without_allow_removals(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)
    fp = outline.outline_fingerprint(PROPS)
    without_11 = FULL[1:]

    preview = outline.update_assignment_outline("1", "2", without_11)
    refused = outline.update_assignment_outline("1", "2", without_11, fp, confirm_write=True)
    allowed = outline.update_assignment_outline(
        "1", "2", without_11, fp, allow_removals=True, confirm_write=True
    )

    assert "would delete" in preview and "`11`" in preview
    assert refused.startswith("Error:") and "`11`" in refused
    assert allowed.startswith("✅")
    assert len(gs.sent) == 1


@pytest.mark.parametrize(
    "questions, message",
    [
        ([{"title": "x", "weight": 1, "crop_rect_list": [_rect(8, 0, 10)]}], "outside the template"),
        ([{"title": "x", "weight": 1, "crop_rect_list": [_rect(1, 20, 10)]}], "y1 < y2"),
        ([{"title": "x", "weight": 1, "crop_rect_list": [_rect(1, 0, 101)]}], "0-100"),
        ([{"title": "x", "weight": 1, "crop_rect_list": []}], "at least one region"),
        ([{"title": "x", "weight": -1, "crop_rect_list": [_rect(1, 0, 10)]}], "weight"),
        ([{"question_id": "99", "weight": 1, "crop_rect_list": [_rect(1, 0, 10)]}], "not a question"),
        (
            [{"question_id": "11", "weight": 1, "crop_rect_list": [_rect(1, 0, 10)]},
             {"question_id": "11", "weight": 1, "crop_rect_list": [_rect(1, 0, 10)]}],
            "used twice",
        ),
        (
            [{"weight": 3, "children": [{"weight": 1, "crop_rect_list": [_rect(1, 0, 10)]}]}],
            "sum of its children",
        ),
        (
            [{"children": [{"weight": 1, "crop_rect_list": [_rect(1, 0, 10)],
                            "children": [{"weight": 1}]}]}],
            "nested only one level",
        ),
        ([{"weight": 1, "crop_rect_list": [_rect(1, 0, 10)], "points": 1}], "unknown field"),
    ],
)
def test_invalid_outlines_are_refused_before_anything_is_sent(monkeypatch, questions, message) -> None:
    gs = FakeGradescope(monkeypatch)

    result = outline.update_assignment_outline("1", "2", questions)

    assert result.startswith("Error: outline refused"), result
    assert message in result
    assert gs.sent == []


def test_rejected_save_is_reported(monkeypatch) -> None:
    FakeGradescope(monkeypatch, status=422)
    fp = outline.outline_fingerprint(PROPS)

    result = outline.update_assignment_outline("1", "2", FULL, fp, confirm_write=True)

    assert result.startswith("Error: Gradescope rejected the outline (status 422)")
