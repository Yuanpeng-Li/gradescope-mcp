import copy
import json
from types import SimpleNamespace

from gradescope_mcp import operations
from gradescope_mcp.tools import grading_plans as plans


def world(monkeypatch):
    states = {
        s: {
            "question": {
                "id": 2,
                "weight": 10,
                "scoring_type": "positive",
                "floor": True,
                "ceiling": True,
            },
            "submission": {"id": int(s), "graded": False},
            "evaluation": {"points": 0, "comments": ""},
            "rubric_items": [],
            "urls": {
                "save_grade": f"/courses/1/questions/2/submissions/{s}/save_grade"
            },
        }
        for s in ("3", "4")
    }
    sent = []
    monkeypatch.setattr(
        plans.grading,
        "_get_grading_context",
        lambda c, q, s: {"props": copy.deepcopy(states[s])},
    )

    def post(ctx, payload):
        sid = str(ctx["props"]["submission"]["id"])
        sent.append(sid)
        states[sid]["evaluation"] = dict(payload["question_submission_evaluation"])
        states[sid]["submission"]["graded"] = True
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(plans.grading, "_post_save_grade", post)
    monkeypatch.setattr(
        plans.grading,
        "_read_back_grade",
        lambda *a: {"mismatches": [], "score": 5, "graded": True},
    )
    prepared = json.loads(
        plans.prepare_grade_plan(
            "1", "2", [{"submission_id": s, "point_adjustment": 5} for s in states]
        )
    )
    return prepared, states, sent


def test_plan_replay_does_not_resend(monkeypatch):
    p, states, sent = world(monkeypatch)
    assert not sent
    result = json.loads(
        plans.execute_grade_plan(p["operation_token"], p["request_digest"], True)
    )
    assert result["status"] == "completed" and sent == ["3", "4"]
    plans.execute_grade_plan(p["operation_token"], p["request_digest"], True)
    assert sent == ["3", "4"]


def test_snapshot_change_refuses_write(monkeypatch):
    p, states, sent = world(monkeypatch)
    states["3"]["question"]["ceiling"] = False
    r = json.loads(
        plans.execute_grade_plan(p["operation_token"], p["request_digest"], True)
    )
    assert r["attention_rows"][0]["state"] == "stale" and not sent


def test_unknown_save_is_not_repeated(monkeypatch):
    p, states, sent = world(monkeypatch)

    def timeout(*a):
        sent.append("attempt")
        raise TimeoutError("lost response")

    monkeypatch.setattr(plans.grading, "_post_save_grade", timeout)
    for _ in range(2):
        r = json.loads(
            plans.execute_grade_plan(p["operation_token"], p["request_digest"], True)
        )
        assert r["status"] == "needs_review"
    assert sent == ["attempt"]


def test_cancel_and_calibration(monkeypatch):
    p, _, sent = world(monkeypatch)
    assert (
        json.loads(plans.calibrate_grade_plan(p["operation_token"], {"3": 5}))[
            "mean_absolute_error"
        ]
        == 0
    )
    operations.cancel_operation(p["operation_token"])
    r = json.loads(
        plans.execute_grade_plan(p["operation_token"], p["request_digest"], True)
    )
    assert r["status"] == "cancelled" and not sent


def test_plan_digest_tamper(monkeypatch):
    p, _, sent = world(monkeypatch)
    op = operations.load(p["operation_token"])
    op["request"]["grades"][0]["point_adjustment"] = 9
    operations.save(op)
    assert plans.execute_grade_plan(
        p["operation_token"], p["request_digest"], True
    ).startswith("Error:")
    assert not sent


def test_cancel_during_final_read_prevents_send(monkeypatch):
    p, states, sent = world(monkeypatch)
    read = plans.grading._get_grading_context

    def cancelling_read(*args):
        operations.cancel_operation(p["operation_token"])
        return read(*args)

    monkeypatch.setattr(plans.grading, "_get_grading_context", cancelling_read)
    result = json.loads(
        plans.execute_grade_plan(p["operation_token"], p["request_digest"], True)
    )
    assert result["status"] == "cancelled" and not sent


def test_cancel_propagates_to_clone_children():
    child = operations.create("assignment", {}, {}, [])
    rubric = operations.create("rubric", {}, {}, [])
    parent = operations.create("assignment_clone", {}, {}, [])
    parent["creation_operation"] = child["operation_token"]
    parent["rubric_results"] = [{"operation_token": rubric["operation_token"]}]
    operations.save(parent)
    operations.cancel_operation(parent["operation_token"])
    assert all(
        operations.cancelled(p["operation_token"]) for p in (parent, child, rubric)
    )


def test_ungraded_readback_is_not_verified(monkeypatch):
    p, _, sent = world(monkeypatch)
    monkeypatch.setattr(
        plans.grading,
        "_read_back_grade",
        lambda *a: {"graded": False, "mismatches": []},
    )
    result = json.loads(
        plans.execute_grade_plan(p["operation_token"], p["request_digest"], True)
    )
    assert result["attention_rows"][0]["state"] == "accepted_unverified"
    assert sent == ["3"]


def _lock_worker(cache, started, acquired):
    import os

    os.environ["GRADESCOPE_MCP_CACHE_DIR"] = cache
    from gradescope_mcp.tools.safety import question_write_lock

    started.set()
    with question_write_lock("1", "2"):
        acquired.set()


def test_question_lock_coordinates_processes(tmp_path, monkeypatch):
    import multiprocessing
    from gradescope_mcp.tools.safety import question_write_lock

    cache = str(tmp_path / "lock-cache")
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", cache)
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    acquired = ctx.Event()
    child = ctx.Process(target=_lock_worker, args=(cache, started, acquired))
    try:
        with question_write_lock("1", "2"):
            with question_write_lock("1", "2"):  # Nested workflow calls stay reentrant.
                child.start()
                assert started.wait(5)
                assert not acquired.wait(0.15)
        assert acquired.wait(5)
        child.join(5)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)


def test_journal_pagination_does_not_duplicate_full_grade_payload(monkeypatch):
    p, _, _ = world(monkeypatch)
    result = json.loads(operations.get_operation(p["operation_token"], False, 0, 1))
    assert result["total_rows"] == 2 and result["next_offset"] == 1
    assert "request" not in result and "payload" not in result["rows"][0]
    detailed = json.loads(operations.get_operation(p["operation_token"], True, 1, 1))
    assert detailed["rows"][0]["submission_id"] == "4"
    assert "grades" not in detailed["request"] and set(detailed["snapshots"]) == {"4"}
