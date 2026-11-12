import copy
import json

import pytest

from gradescope_mcp import operations as ops
from gradescope_mcp.tools import rubric_plans as rp


def test_rubric_plan_create_and_update(monkeypatch):
    current = [{"id": "1", "description": "Old", "weight": 2.0}]
    monkeypatch.setattr(rp, "_rubric", lambda *a: copy.deepcopy(current))
    calls = []

    def update(c, q, r, **kwargs):
        current[0].update(description=kwargs["description"], weight=kwargs["weight"])
        calls.append("update")
        return "ok"

    def create(c, q, description, weight, *a):
        current.append({"id": "2", "description": description, "weight": weight})
        calls.append("create")
        return "ok"

    monkeypatch.setattr(rp.grading, "update_rubric_item", update)
    monkeypatch.setattr(rp.grading, "create_rubric_item", create)
    p = json.loads(
        rp.prepare_rubric_plan(
            "1",
            "2",
            [
                {"id": "1", "description": "Updated", "weight": 3},
                {"description": "New", "weight": 1},
            ],
        )
    )
    assert not calls
    result = json.loads(
        rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True)
    )
    assert result["status"] == "completed" and calls == ["update", "create"]
    rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True)
    assert len(calls) == 2


def test_rubric_source_change_refused(monkeypatch):
    rows = []
    monkeypatch.setattr(rp, "_rubric", lambda *a: rows.copy())
    p = json.loads(
        rp.prepare_rubric_plan("1", "2", [{"description": "x", "weight": 1}])
    )
    rows.append({"id": "9", "description": "Other", "weight": 2})
    assert "changed" in rp.execute_rubric_plan(
        p["operation_token"], p["request_digest"], True
    )


def test_pre_submission_editor_normalizes_question_urls():
    import html
    from bs4 import BeautifulSoup
    from gradescope_mcp.tools.rubric_data import assignment_rubric_props

    props = {
        "course": {"id": 1},
        "assignment": {"id": 7},
        "questions": [
            {
                "id": 2,
                "weight": "0.0",
                "scoring_type": "positive",
                "floor": True,
                "ceiling": False,
                "children": None,
            }
        ],
        "rubric_items": [
            {"id": 4, "question_id": 2, "description": "Correct", "weight": "1.0"},
            {"id": 5, "question_id": 99},
        ],
        "urls": {
            "question_urls": {
                "2": {
                    "save_grading_options": "/courses/1/questions/2/save_grading_options"
                }
            }
        },
    }
    soup = BeautifulSoup(
        '<div data-react-class="AssignmentRubric" data-react-props="'
        + html.escape(json.dumps(props), quote=True)
        + '"></div>',
        "html.parser",
    )
    result = assignment_rubric_props(soup, "1", "2")
    assert result["question"]["weight"] == "0.0" and len(result["rubric_items"]) == 1
    assert result["urls"]["save_grading_options"].endswith("/2/save_grading_options")


# ---------------------------------------------------------------------------
# resume_rubric_plan: interrupted plans continue without duplicate writes
# ---------------------------------------------------------------------------


class FakeRubric:
    """In-memory question rubric with a call log and failure injection."""

    def __init__(self, items):
        self.items = [dict(i) for i in items]
        self.next_id = 100
        self.calls = []
        self.fail_after = set()  # actions that apply, then raise

    def read(self, *a):
        return copy.deepcopy(self.items)

    def create(self, c, q, description, weight, *a):
        self.calls.append(("create", description))
        self.next_id += 1
        self.items.append(
            {"id": str(self.next_id), "description": description, "weight": float(weight)}
        )
        if "create" in self.fail_after:
            raise RuntimeError("connection reset")
        return "ok"

    def update(self, c, q, rid, **kw):
        self.calls.append(("update", rid))
        for item in self.items:
            if item["id"] == rid:
                item.update(description=kw["description"], weight=float(kw["weight"]))
        if "update" in self.fail_after:
            raise RuntimeError("timeout")
        return "ok"

    def delete(self, c, q, rid, *a):
        self.calls.append(("delete", rid))
        self.items = [i for i in self.items if i["id"] != rid]
        if "delete" in self.fail_after:
            raise RuntimeError("timeout")
        return "ok"


@pytest.fixture
def rubric(monkeypatch):
    fake = FakeRubric([
        {"id": "1", "description": "Keep", "weight": 1.0},
        {"id": "2", "description": "Old", "weight": 2.0},
        {"id": "3", "description": "Drop", "weight": 3.0},
    ])
    monkeypatch.setattr(rp, "_rubric", fake.read)
    monkeypatch.setattr(rp.grading, "create_rubric_item", fake.create)
    monkeypatch.setattr(rp.grading, "update_rubric_item", fake.update)
    monkeypatch.setattr(rp.grading, "delete_rubric_item", fake.delete)
    return fake


def _plan(items, remove_missing=False):
    return json.loads(rp.prepare_rubric_plan("1", "2", items, remove_missing))


def _two_creates():
    return _plan([
        {"id": "1", "description": "Keep", "weight": 1},
        {"id": "2", "description": "Old", "weight": 2},
        {"id": "3", "description": "Drop", "weight": 3},
        {"description": "New A", "weight": 1},
        {"description": "New B", "weight": 0.5},
    ])


_UNSET = object()


def _mark(p, index, state, status="running", sent=_UNSET):
    """Rewrite the journal as an interrupted run would have left it."""
    op = ops.load(p["operation_token"])
    op["status"] = status
    op["rows"][index]["state"] = state
    if sent is not _UNSET:
        op["rows"][index]["sent"] = sent
    ops.save(op)


def _resume(p, confirm=True, retry_unconfirmed=False):
    out = rp.resume_rubric_plan(
        p["operation_token"], p["request_digest"], confirm, retry_unconfirmed
    )
    return json.loads(out) if out.startswith("{") else out


def test_create_failure_then_resume_reconciles_without_duplicate(rubric):
    p = _two_creates()
    rubric.fail_after = {"create"}
    first = json.loads(rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True))
    assert first["status"] == "needs_review"
    assert [r["state"] for r in first["rows"]] == ["unknown", "pending"]
    assert first["progress"] == {
        "total": 2, "verified": 0, "failed": 1, "pending": 1, "current_index": 0
    }

    # A plain re-run is refused and points at the resume tool.
    again = rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True)
    assert again.startswith("Error:") and "tool_resume_rubric_plan" in again

    rubric.fail_after = set()
    result = _resume(p)
    assert result["status"] == "completed"
    assert rubric.calls == [("create", "New A"), ("create", "New B")]  # A sent once
    a, b = result["rows"]
    assert a["state"] == "verified" and a["reconciled"] == "verified"
    assert a["created_id"] == "101" and "error" not in a
    assert b["state"] == "verified" and b["created_id"] == "102"
    assert [i["description"] for i in rubric.items].count("New A") == 1
    assert result["progress"] == {
        "total": 2, "verified": 2, "failed": 0, "pending": 0, "current_index": None
    }
    stored = json.loads(ops.get_operation(p["operation_token"]))
    assert stored["progress"]["verified"] == 2


def test_interrupted_create_that_never_reached_gradescope_is_sent(rubric):
    p = _two_creates()
    _mark(p, 0, "sending", sent=False)  # crashed before the request

    result = _resume(p)

    assert result["status"] == "completed"
    assert rubric.calls == [("create", "New A"), ("create", "New B")]
    assert result["rows"][0]["reconciled"] == "send"


def test_two_matching_new_items_stop_for_review(rubric):
    p = _two_creates()
    _mark(p, 0, "sending")
    rubric.items += [
        {"id": "50", "description": "New A", "weight": 1.0},
        {"id": "51", "description": "New A", "weight": 1.0},
    ]

    result = _resume(p)

    assert result["status"] == "needs_review"
    assert rubric.calls == []
    row = result["rows"][0]
    assert row["state"] == "needs_review"
    assert "2 matching new items" in row["review_reason"]
    assert result["progress"]["failed"] == 1
    assert result["progress"]["current_index"] == 0


def test_unrelated_new_item_stops_for_review(rubric):
    p = _two_creates()
    _mark(p, 0, "sending")
    rubric.items.append({"id": "60", "description": "Someone else's", "weight": 4.0})

    result = _resume(p)

    assert result["status"] == "needs_review" and rubric.calls == []
    assert "did not create" in result["rows"][0]["review_reason"]


def test_changed_existing_item_stops_create_reconciliation(rubric):
    p = _two_creates()
    _mark(p, 0, "unknown", "needs_review")
    rubric.items[0]["weight"] = 9.0

    result = _resume(p)

    assert result["status"] == "needs_review" and rubric.calls == []


def test_update_and_delete_reconciliation(rubric):
    p = _plan([{"id": "2", "description": "New text", "weight": 2.5}], remove_missing=True)
    assert [r["action"] for r in p["actions"]] == ["update", "delete", "delete"]
    rubric.fail_after = {"update"}
    first = json.loads(rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True))
    assert first["rows"][0]["state"] == "unknown"

    rubric.fail_after = set()
    result = _resume(p)

    assert result["status"] == "completed"
    # The applied update is not re-sent; both deletes run once.
    assert rubric.calls == [("update", "2"), ("delete", "1"), ("delete", "3")]
    assert rubric.items == [{"id": "2", "description": "New text", "weight": 2.5}]


def test_unapplied_update_is_resent(rubric):
    p = _plan([{"id": "2", "description": "New text", "weight": 2.5}])
    _mark(p, 0, "sending")

    result = _resume(p)

    assert result["status"] == "completed" and rubric.calls == [("update", "2")]


def test_update_target_with_third_value_needs_review(rubric):
    p = _plan([{"id": "2", "description": "New text", "weight": 2.5}])
    _mark(p, 0, "unknown", "needs_review")
    rubric.items[1]["description"] = "Edited by hand"

    result = _resume(p)

    assert result["status"] == "needs_review" and rubric.calls == []


def test_applied_delete_is_verified_without_resend(rubric):
    p = _plan(
        [
            {"id": "1", "description": "Keep", "weight": 1},
            {"id": "2", "description": "Old", "weight": 2},
        ],
        remove_missing=True,
    )
    rubric.fail_after = {"delete"}
    first = json.loads(rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True))
    assert first["rows"][0]["state"] == "unknown"
    rubric.fail_after = set()

    result = _resume(p)

    assert result["status"] == "completed" and rubric.calls == [("delete", "3")]


@pytest.mark.parametrize("state", ["sending", "unknown"])
@pytest.mark.parametrize("sent", [True, _UNSET])
def test_possibly_sent_create_absent_from_rubric_is_not_resent(rubric, state, sent):
    """Absence is no proof a timed-out POST failed: it may still land."""
    p = _two_creates()
    _mark(p, 0, state, sent=sent)

    blocked = _resume(p)

    assert blocked["status"] == "needs_review" and rubric.calls == []
    assert "retry_unconfirmed_creates=True" in blocked["rows"][0]["review_reason"]

    # Only an explicit retry, after the user confirmed it is still missing.
    result = _resume(p, retry_unconfirmed=True)
    assert result["status"] == "completed"
    assert rubric.calls == [("create", "New A"), ("create", "New B")]


def test_possibly_sent_create_that_landed_late_is_reconciled(rubric):
    p = _two_creates()
    _mark(p, 0, "unknown", sent=True)
    assert _resume(p)["status"] == "needs_review"

    rubric.items.append({"id": "90", "description": "New A", "weight": 1.0})
    result = _resume(p)

    assert result["status"] == "completed"
    assert result["rows"][0]["created_id"] == "90"
    assert rubric.calls == [("create", "New B")]


def test_stale_row_is_sent_only_once_the_rubric_matches_again(rubric):
    p = _two_creates()
    _mark(p, 0, "stale", "needs_review", sent=False)
    rubric.items.append({"id": "70", "description": "Intruder", "weight": 1.0})

    blocked = _resume(p)
    assert blocked["status"] == "needs_review" and rubric.calls == []

    rubric.items.pop()  # the intruder is removed by hand
    result = _resume(p)
    assert result["status"] == "completed"
    assert rubric.calls == [("create", "New A"), ("create", "New B")]


def test_resume_requires_confirmation_and_matching_digest(rubric):
    p = _two_creates()
    preview = _resume(p, confirm=False)
    assert preview.startswith("Write confirmation required for `resume_rubric_plan`")
    assert "New A" in preview and rubric.calls == []
    bad = rp.resume_rubric_plan(p["operation_token"], "0" * 64, True)
    assert bad.startswith("Error:") and rubric.calls == []


def test_resume_of_prepared_plan_executes_and_completed_is_noop(rubric):
    p = _two_creates()
    result = _resume(p)
    assert result["status"] == "completed" and len(rubric.calls) == 2
    again = _resume(p)
    assert again["status"] == "completed" and len(rubric.calls) == 2


def test_resume_of_prepared_plan_refuses_changed_rubric(rubric):
    p = _two_creates()
    rubric.items.append({"id": "70", "description": "Intruder", "weight": 1.0})
    out = rp.resume_rubric_plan(p["operation_token"], p["request_digest"], True)
    assert "changed" in out and rubric.calls == []


def test_cancelled_plan_cannot_be_resumed(rubric):
    p = _two_creates()
    ops.cancel_operation(p["operation_token"])
    out = rp.resume_rubric_plan(p["operation_token"], p["request_digest"], True)
    assert out.startswith("Error:") and "cancelled" in out and rubric.calls == []


def test_verified_create_without_id_is_flagged(rubric):
    p = _two_creates()
    _mark(p, 0, "verified", "needs_review")  # hand-edited journal
    result = _resume(p)
    assert result["status"] == "needs_review" and rubric.calls == []


def test_prepare_reports_markup_warnings_without_blocking(rubric):
    p = _plan([
        {"description": "Uses $$$$x^2$$$$ twice", "weight": 1},
        {"description": "Fine $x$", "weight": 1},
    ])
    assert p["operation_token"]
    assert list(p["markup_warnings"]) == ["items[0].description"]
    assert "markup_warnings" in p["notice"]
    clean = _plan([{"description": "Plain", "weight": 1}])
    assert clean["markup_warnings"] == {}
    assert ops.load(clean["operation_token"])["progress"]["pending"] == 1


def test_running_rubric_operation_reports_executor_active(rubric, monkeypatch):
    """Polling mid-write must not call a live rubric run abandoned."""
    p = _two_creates()
    seen = []
    real_send = rp._send

    def spying_send(c, q, row):
        seen.append(json.loads(ops.get_operation(p["operation_token"])))
        again = rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True)
        seen.append(again)
        return real_send(c, q, row)

    monkeypatch.setattr(rp, "_send", spying_send)
    result = json.loads(
        rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True)
    )

    assert result["status"] == "completed"
    polled = seen[0]
    assert polled["status"] == "running" and polled["executor_active"] is True
    assert "note" not in polled
    assert seen[1].startswith("Error: This rubric operation is already running")
    assert rubric.calls == [("create", "New A"), ("create", "New B")]
    assert not ops.is_running(p["operation_token"])


def test_abandoned_rubric_run_points_to_rubric_resume(rubric):
    p = _two_creates()
    _mark(p, 0, "sending", sent=False)
    polled = json.loads(ops.get_operation(p["operation_token"]))
    assert polled["executor_active"] is False
    assert "tool_resume_rubric_plan" in polled["note"]
    assert "tool_resume_operation " not in polled["note"]
