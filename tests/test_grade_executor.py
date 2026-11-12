"""Grade-plan executor: concurrency, progress, background runs, resume, score
verification and compact output. Offline; Gradescope is faked."""

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from gradescope_mcp import auth, operations
from gradescope_mcp.tools import grading_plans as plans


class World:
    """Fake Gradescope: per-submission grading pages, save and read-back."""

    def __init__(self, monkeypatch, sids=("3", "4"), delays=None):
        self.states = {
            s: {
                "question": {
                    "id": 2,
                    "weight": 10,
                    "scoring_type": "positive",
                    "floor": True,
                    "ceiling": True,
                },
                "submission": {"id": int(s), "graded": False, "score": None},
                "evaluation": {"points": 0, "comments": ""},
                "rubric_items": [],
                "urls": {
                    "save_grade": f"/courses/1/questions/2/submissions/{s}/save_grade"
                },
            }
            for s in sids
        }
        self.sent, self.reads = [], []
        self.delays = delays or {}
        self.score_offset = 0.0  # Gradescope's score minus the true score
        self.post_hook = None
        self.read_hook = None
        self.guard = threading.Lock()
        monkeypatch.setattr(plans.grading, "_get_grading_context", self.read)
        monkeypatch.setattr(plans.grading, "_post_save_grade", self.post)

    def read(self, c, q, s):
        with self.guard:
            self.reads.append(s)
        if self.read_hook:
            self.read_hook(s)
        time.sleep(self.delays.get(s, 0))
        with self.guard:
            return {"props": copy.deepcopy(self.states[s])}

    def post(self, ctx, payload):
        sid = str(ctx["props"]["submission"]["id"])
        with self.guard:
            self.sent.append(sid)
        if self.post_hook:
            result = self.post_hook(sid, payload)
            if result is not None:
                return result
        self.land(sid, payload)
        return SimpleNamespace(status_code=200)

    def land(self, sid, payload):
        with self.guard:
            ev = dict(payload["question_submission_evaluation"])
            self.states[sid]["evaluation"] = ev
            self.states[sid]["submission"]["graded"] = True
            self.states[sid]["submission"]["score"] = (ev["points"] or 0) + self.score_offset

    def prepare(self, rows=None, **kw):
        rows = rows or [{"submission_id": s, "point_adjustment": 5} for s in self.states]
        return json.loads(plans.prepare_grade_plan("1", "2", rows, **kw))


def _execute(p, **kw):
    return json.loads(
        plans.execute_grade_plan(p["operation_token"], p["request_digest"], True, **kw)
    )


def _resume(p, **kw):
    return json.loads(
        plans.resume_operation(p["operation_token"], p["request_digest"], True, **kw)
    )


# ---------------------------------------------------------------------------
# auth helpers for worker threads
# ---------------------------------------------------------------------------


def test_run_with_call_state_captures_and_resets_worker_state():
    conn = object()

    def work():
        auth._local.writes = 2
        auth._local.pending_write = True
        auth._local.expired = conn
        return "ok"

    result, exc, state = auth.run_with_call_state(work)
    assert (result, exc) == ("ok", None)
    assert state == auth.ThreadCallState(conn, 3)  # pending redirect counts
    assert auth._local.writes == 0 and auth._local.expired is None

    _, exc, _ = auth.run_with_call_state(lambda: 1 / 0)
    assert isinstance(exc, ZeroDivisionError)


def test_merge_call_state_adds_writes_and_keeps_first_expiry():
    first, second = object(), object()
    auth._reset_call_state()
    try:
        auth.merge_call_state(auth.ThreadCallState(first, 1))
        auth.merge_call_state(auth.ThreadCallState(second, 2))
        auth.merge_call_state(auth.ThreadCallState(None, 0))
        assert auth._local.expired is first and auth._local.writes == 3
    finally:
        auth._reset_call_state()


@pytest.mark.parametrize("writes, runs", [(0, 2), (1, 1)])
def test_session_recovery_sees_pooled_expiry_and_writes(monkeypatch, writes, runs):
    """An expiry on a pool thread triggers recovery; an accepted write on a
    pool thread prevents the re-run."""
    conn = object()
    resets, calls = [], []
    monkeypatch.setattr(auth, "reset_connection", lambda expired=None: resets.append(expired))

    def item(i):
        if i == 1 and len(calls) == 1:
            auth._local.writes = getattr(auth._local, "writes", 0) + writes
            auth._local.expired = conn
        return i

    @auth.with_session_recovery
    def tool():
        calls.append(1)
        return str(plans._concurrent_map(item, range(4), 4))

    assert tool().startswith("[0, 1, 2, 3]")
    assert len(calls) == runs and resets == [conn]


# ---------------------------------------------------------------------------
# prepare: concurrent reads, compact rows, markup warnings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected", [(None, 4), ("0", 1), ("2", 2), ("99", 8), ("x", 4)]
)
def test_read_concurrency_env_is_clamped(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(plans.READ_CONCURRENCY_ENV, raising=False)
    else:
        monkeypatch.setenv(plans.READ_CONCURRENCY_ENV, raw)
    assert plans.read_concurrency() == expected


@pytest.mark.parametrize("raw, expected", [(None, 1), ("0", 1), ("3", 3), ("9", 4)])
def test_write_concurrency_env_is_clamped(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(plans.WRITE_CONCURRENCY_ENV, raising=False)
    else:
        monkeypatch.setenv(plans.WRITE_CONCURRENCY_ENV, raw)
    assert plans.write_concurrency() == expected


def test_prepare_reads_concurrently_and_keeps_input_order(monkeypatch):
    sids = [str(i) for i in range(10, 18)]
    # Earlier rows are slower, so completion order is the reverse.
    world = World(monkeypatch, sids, {s: 0.02 * (18 - int(s)) for s in sids})
    active, peak = [0], [0]
    lock = threading.Lock()

    def hook(_s):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])

    def tracked(c, q, s):
        try:
            return World.read(world, c, q, s)
        finally:
            with lock:
                active[0] -= 1

    world.read_hook = hook
    monkeypatch.setattr(plans.grading, "_get_grading_context", tracked)
    p = world.prepare()
    assert [r["submission_id"] for r in p["rows"]] == sids
    assert 1 < peak[0] <= plans.DEFAULT_READ_CONCURRENCY
    op = operations.load(p["operation_token"])
    assert [r["submission_id"] for r in op["rows"]] == sids
    assert set(op["snapshots"]) == set(sids)


def test_prepare_reports_the_first_failing_row_deterministically(monkeypatch):
    sids = [str(i) for i in range(10, 16)]
    world = World(monkeypatch, sids, {"11": 0.15})

    def hook(s):
        if s in ("11", "13"):  # 13 fails first in time, 11 is earlier in order
            if s == "13":
                raise ValueError("boom 13")
            time.sleep(0.1)
            raise ValueError("boom 11")

    world.read_hook = hook
    result = plans.prepare_grade_plan(
        "1", "2", [{"submission_id": s, "point_adjustment": 1} for s in sids]
    )
    assert result.startswith("Error: Cannot prepare grade plan: submission 11: boom 11")


def test_prepare_auth_error_is_reported_as_authentication_error(monkeypatch):
    world = World(monkeypatch, ("3", "4", "5"))

    def hook(s):
        if s == "4":
            raise auth.SessionExpiredError("expired")

    world.read_hook = hook
    assert world_prepare_text(world).startswith("Authentication error:")


def world_prepare_text(world):
    return plans.prepare_grade_plan(
        "1", "2", [{"submission_id": s, "point_adjustment": 1} for s in world.states]
    )


def test_prepare_returns_compact_rows_and_markup_warnings(monkeypatch):
    world = World(monkeypatch)
    long = "x" * 400
    p = world.prepare(
        [
            {"submission_id": "3", "point_adjustment": 5, "comment": "$$$$a$$$$"},
            {"submission_id": "4", "point_adjustment": 4, "comment": long, "confidence": 0.7},
        ],
        evidence={"3": {"page": 1}},
    )
    r3, r4 = p["rows"]
    assert "payload" not in r3 and "evidence" not in r3
    assert r3["projected_score"] == 5 and r3["point_adjustment"] == 5
    assert r3["rubric_item_ids"] == [] and r3["markup_warnings"]
    assert "markup_warnings" not in r4 and r4["needs_review"] is True
    assert len(r4["comment_preview"]) == plans.COMMENT_PREVIEW_CHARS
    assert r4["comment_chars"] == 400
    assert p["summary"] == {
        "rows": 2,
        "pending": 2,
        "blocked": 0,
        "needs_review": 1,
        "markup_warning_rows": 1,
    }
    assert "advisory" in p["notice"]
    # The journal keeps the full rows, including the warnings.
    op = operations.load(p["operation_token"])
    assert op["rows"][0]["markup_warnings"] and op["rows"][0]["payload"]

    verbose = world.prepare(
        [{"submission_id": "3", "point_adjustment": 5}], verbose=True
    )
    assert verbose["rows"][0]["payload"]["question_submission_evaluation"]["points"] == 5


# ---------------------------------------------------------------------------
# execute: score verification, progress, compact output
# ---------------------------------------------------------------------------


def test_execute_verifies_score_and_records_progress(monkeypatch):
    world = World(monkeypatch)
    p = world.prepare()
    result = _execute(p)
    assert result["status"] == "completed" and world.sent == ["3", "4"]
    assert result["attention_rows"] == [] and "rows" not in result
    progress = result["progress"]
    assert progress["total"] == 2 and progress["checked"] == 2
    assert progress["written"] == 2 and progress["verified"] == 2
    assert progress["failed"] == 0 and progress["current_submission_id"] == "4"
    assert progress["started_at"] <= progress["updated_at"]
    op = operations.load(p["operation_token"])
    assert all(r["state"] == "verified" and r["actual_score"] == 5 for r in op["rows"])

    info = json.loads(operations.get_operation(p["operation_token"]))
    assert info["progress"]["verified"] == 2 and info["executor_active"] is False

    full = _execute(p, verbose=True)
    assert full["rows"][0]["state"] == "verified" and world.sent == ["3", "4"]


def test_score_mismatch_is_not_verified(monkeypatch):
    world = World(monkeypatch)
    world.score_offset = -1.0
    p = world.prepare()
    result = _execute(p)
    assert result["status"] == "needs_review" and world.sent == ["3"]
    row = result["attention_rows"][0]
    assert row["state"] == "score_mismatch"
    assert row["actual_score"] == 4 and row["projected_score"] == 5
    assert result["progress"]["failed"] == 1


def test_missing_readback_score_is_accepted_unverified(monkeypatch):
    world = World(monkeypatch)
    real_land = world.land

    def land_without_score(sid, payload):
        real_land(sid, payload)
        world.states[sid]["submission"]["score"] = None
        world.states[sid]["submission"]["graded"] = True

    world.land = land_without_score
    result = _execute(world.prepare())
    row = result["attention_rows"][0]
    assert row["state"] == "accepted_unverified"
    assert "numeric score" in row["verification_note"]


def test_reconcile_path_also_compares_score(monkeypatch):
    """A row Gradescope already holds is verified only with the right score."""
    world = World(monkeypatch)
    p = world.prepare()
    world.score_offset = 2.0
    world.land("3", operations.load(p["operation_token"])["rows"][0]["payload"])
    result = _execute(p)
    assert world.sent == []
    row = result["attention_rows"][0]
    assert row["state"] == "score_mismatch" and row["reconciled"] is True


def test_attention_rows_are_capped(monkeypatch):
    sids = [str(i) for i in range(100, 126)]
    world = World(monkeypatch, sids)
    p = world.prepare(
        [{"submission_id": s, "point_adjustment": 1, "confidence": 0.1} for s in sids]
    )
    assert p["summary"]["blocked"] == 26
    result = _execute(p)
    assert result["status"] == "completed_with_blocked_rows" and not world.sent
    assert len(result["attention_rows"]) == plans.ATTENTION_LIMIT
    assert result["attention_omitted"] == 26 - plans.ATTENTION_LIMIT
    assert result["attention_rows"][0]["blocked_reason"] == "low_confidence"


def test_preview_is_compact_and_sends_nothing(monkeypatch):
    world = World(monkeypatch)
    p = world.prepare()
    text = plans.execute_grade_plan(p["operation_token"], p["request_digest"])
    assert text.startswith("Write confirmation required") and not world.sent
    assert '"payload"' not in text and "rows to re-check and write" in text


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def _lost_response(world):
    def hook(sid, payload):
        world.post_hook = None
        raise TimeoutError("lost response")

    world.post_hook = hook


def test_execute_stops_at_uncertain_row_and_resume_resends_unlanded_write(monkeypatch):
    world = World(monkeypatch)
    p = world.prepare()
    _lost_response(world)  # first POST raises before anything lands
    assert _execute(p)["status"] == "needs_review" and world.sent == ["3"]
    again = _execute(p)  # execute never re-sends an uncertain row
    assert again["status"] == "needs_review" and world.sent == ["3"]
    assert "tool_resume_operation" in again["attention_rows"][0]["attention"]

    preview = plans.resume_operation(p["operation_token"], p["request_digest"])
    assert preview.startswith("Write confirmation required") and world.sent == ["3"]
    assert "uncertain rows to re-read first: 1" in preview

    result = _resume(p)
    assert result["status"] == "completed" and world.sent == ["3", "3", "4"]
    op = operations.load(p["operation_token"])
    assert op["rows"][0]["resumed_from"] == "unknown"
    assert result["progress"]["mode"] == "resume"


def test_resume_reconciles_landed_write_without_resending(monkeypatch):
    world = World(monkeypatch)
    p = world.prepare()

    def landed_but_lost(sid, payload):
        world.post_hook = None
        world.land(sid, payload)
        raise TimeoutError("lost response after save")

    world.post_hook = landed_but_lost
    assert _execute(p)["status"] == "needs_review"
    result = _resume(p)
    assert result["status"] == "completed" and world.sent == ["3", "4"]
    row = operations.load(p["operation_token"])["rows"][0]
    assert row["state"] == "verified" and row["reconciled"] is True


def test_resume_marks_changed_uncertain_row_stale(monkeypatch):
    world = World(monkeypatch)
    p = world.prepare()
    _lost_response(world)
    _execute(p)
    world.states["3"]["evaluation"] = {"points": 1, "comments": "by hand"}
    world.states["3"]["submission"]["graded"] = True
    result = _resume(p)
    # Row 3 is not overwritten; the reconciliation sent nothing, so the
    # independent row 4 still goes ahead.
    assert result["status"] == "needs_review" and world.sent == ["3", "4"]
    row = result["attention_rows"][0]
    assert row["state"] == "stale" and row["resumed_from"] == "unknown"
    assert world.states["3"]["evaluation"]["comments"] == "by hand"
    assert result["status_reason"] == "rows need human review: 1 stale"


def test_resume_keeps_score_mismatch_flagged_and_continues(monkeypatch):
    world = World(monkeypatch, ("3", "4", "5"))
    p = world.prepare()
    world.score_offset = -1.0
    first = _execute(p)
    assert first["status"] == "needs_review" and world.sent == ["3"]
    world.score_offset = 0.0
    result = _resume(p)
    assert world.sent == ["3", "4", "5"]  # row 3 is never re-sent
    states = [r["state"] for r in operations.load(p["operation_token"])["rows"]]
    assert states == ["score_mismatch", "verified", "verified"]
    assert result["status"] == "needs_review"
    assert result["attention_rows"][0]["actual_score"] == 4


def test_resume_leaves_stale_rows_and_continues_pending(monkeypatch):
    world = World(monkeypatch, ("3", "4", "5"))
    p = world.prepare()
    world.states["3"]["evaluation"] = {"points": 2, "comments": ""}
    assert _execute(p)["status"] == "needs_review" and not world.sent
    reads = len(world.reads)
    result = _resume(p)
    assert world.sent == ["4", "5"]
    assert "3" not in world.reads[reads:]  # stale rows are not touched again
    assert result["status"] == "needs_review"
    assert result["status_reason"] == "rows need human review: 1 stale"
    assert result["progress"]["skipped"] == 1


def test_resume_refuses_other_operation_kinds():
    op = operations.create("rubric_plan", {}, {}, [])
    text = plans.resume_operation(op["operation_token"], op["request_digest"], True)
    assert text.startswith("Error:") and "'rubric_plan'" in text


# ---------------------------------------------------------------------------
# background runs and the run lock
# ---------------------------------------------------------------------------


def test_run_lock_is_exclusive_and_visible():
    op = operations.create("grade_plan", {}, {}, [])
    token = op["operation_token"]
    assert not operations.is_running(token)
    handle = operations.acquire_run(token)
    try:
        assert operations.is_running(token)
        with pytest.raises(operations.OperationRunningError):
            operations.acquire_run(token)
    finally:
        operations.release_run(handle)
    assert not operations.is_running(token)
    operations.release_run(operations.acquire_run(token))


def test_background_execution_reports_running_then_completes(monkeypatch):
    world = World(monkeypatch)
    release = threading.Event()
    p = world.prepare()
    world.read_hook = lambda s: release.wait(5)
    started = _execute(p, background=True)
    assert started["status"] == "running" and started["background"] is True
    token = p["operation_token"]
    info = json.loads(operations.get_operation(token))
    assert info["status"] == "running" and info["executor_active"] is True

    # A second execute or resume is refused at once instead of waiting.
    t0 = time.monotonic()
    second = plans.execute_grade_plan(token, p["request_digest"], True)
    third = plans.resume_operation(token, p["request_digest"], True)
    assert time.monotonic() - t0 < 1
    assert second.startswith("Error:") and "already running" in second
    assert "already running" in third

    release.set()
    assert plans._join_background(token, 5)
    info = json.loads(operations.get_operation(token))
    assert info["status"] == "completed" and info["executor_active"] is False
    assert info["progress"]["verified"] == 2 and world.sent == ["3", "4"]


def test_background_auth_failure_interrupts_and_resume_finishes(monkeypatch):
    world = World(monkeypatch, ("3", "4", "5"))
    p = world.prepare()
    resets = []
    monkeypatch.setattr(auth, "reset_connection", lambda expired=None: resets.append(expired))
    conn = object()

    def expire(s):
        if s == "4":
            world.read_hook = None
            auth._local.expired = conn
            raise auth.SessionExpiredError("expired")

    world.read_hook = expire
    _execute(p, background=True)
    assert plans._join_background(p["operation_token"], 5)
    info = json.loads(operations.get_operation(p["operation_token"]))
    assert info["status"] == "interrupted" and "expired" in info["status_reason"]
    assert info["row_counts"] == {"verified": 1, "pending": 2}
    assert resets == [conn]  # the next call logs in again
    assert not operations.is_running(p["operation_token"])

    result = _resume(p)
    assert result["status"] == "completed" and world.sent == ["3", "4", "5"]


def test_running_journal_without_executor_points_to_resume(monkeypatch):
    world = World(monkeypatch)
    p = world.prepare()
    op = operations.load(p["operation_token"])
    op["status"] = "running"
    operations.save(op)
    info = json.loads(operations.get_operation(p["operation_token"]))
    assert info["executor_active"] is False and "tool_resume_operation" in info["note"]


# ---------------------------------------------------------------------------
# opt-in write concurrency
# ---------------------------------------------------------------------------


def test_parallel_rows_merge_auth_state_into_the_tool_thread(monkeypatch):
    """Writes accepted on pool threads stop with_session_recovery from re-running."""
    monkeypatch.setenv(plans.WRITE_CONCURRENCY_ENV, "2")
    world = World(monkeypatch, ("3", "4", "5", "6"))
    p = world.prepare()
    conn = object()
    resets, calls = [], []
    monkeypatch.setattr(auth, "reset_connection", lambda expired=None: resets.append(expired))

    def accepted_then_expired(sid, payload):
        auth._local.writes = getattr(auth._local, "writes", 0) + 1
        if sid == "4":
            auth._local.expired = conn
        return None

    world.post_hook = accepted_then_expired
    tool = auth.with_session_recovery(
        lambda: calls.append(1) or plans.execute_grade_plan(
            p["operation_token"], p["request_digest"], True
        )
    )
    text = tool()
    assert len(calls) == 1 and resets == [conn]
    assert "accepted 4 write request(s)" in text
    assert sorted(world.sent) == ["3", "4", "5", "6"]


def test_parallel_rows_stop_dispatching_after_a_problem(monkeypatch):
    monkeypatch.setenv(plans.WRITE_CONCURRENCY_ENV, "2")
    sids = [str(i) for i in range(10, 16)]
    world = World(monkeypatch, sids, {"11": 0.2})
    p = world.prepare()
    world.reads.clear()
    world.states["10"]["evaluation"] = {"points": 9, "comments": ""}
    result = _execute(p)
    assert result["status"] == "needs_review"
    assert set(world.reads) == {"10", "11"}  # 11 was in flight and finished
    assert world.sent == ["11"]
    op = operations.load(p["operation_token"])
    assert [r["state"] for r in op["rows"][:3]] == ["stale", "verified", "pending"]


def test_parallel_rows_complete_with_consistent_journal(monkeypatch):
    monkeypatch.setenv(plans.WRITE_CONCURRENCY_ENV, "3")
    sids = [str(i) for i in range(20, 32)]
    world = World(monkeypatch, sids)
    p = world.prepare()
    result = _execute(p)
    assert result["status"] == "completed" and sorted(world.sent) == sids
    assert result["progress"]["verified"] == 12 and result["progress"]["written"] == 12
    assert result["progress"]["write_concurrency"] == 3


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------


def test_background_start_does_not_overwrite_a_newer_journal(monkeypatch):
    """A caller that loaded the plan before another run journaled an uncertain
    row must not reset that row to pending and re-send it."""
    world = World(monkeypatch)
    p = world.prepare()
    real_acquire = operations.acquire_run

    def other_run_finished_first(token):
        op = operations.load(token)
        op["rows"][0]["state"] = "unknown"  # another run lost a response
        operations.save(op)
        return real_acquire(token)

    monkeypatch.setattr(operations, "acquire_run", other_run_finished_first)
    _execute(p, background=True)
    assert plans._join_background(p["operation_token"], 5)
    op = operations.load(p["operation_token"])
    assert op["rows"][0]["state"] == "unknown" and world.sent == []
    assert op["status"] == "needs_review"


def test_reconcile_needs_the_raw_graded_flag(monkeypatch):
    world = World(monkeypatch)
    p = world.prepare()

    def landed_without_flag(sid, payload):
        world.post_hook = None
        world.land(sid, payload)
        world.states[sid]["submission"]["graded"] = None  # score present only
        raise TimeoutError("lost response after save")

    world.post_hook = landed_without_flag
    _execute(p)
    result = _resume(p)
    row = operations.load(p["operation_token"])["rows"][0]
    assert row["state"] == "accepted_unverified" and row["reconciled"] is True
    assert world.sent == ["3", "4"] and result["status"] == "needs_review"


def test_parallel_execute_writes_nothing_while_a_row_is_uncertain(monkeypatch):
    monkeypatch.setenv(plans.WRITE_CONCURRENCY_ENV, "2")
    world = World(monkeypatch, ("3", "4", "5"), {"3": 0.1})
    p = world.prepare()
    op = operations.load(p["operation_token"])
    op["rows"][0]["state"] = "unknown"
    operations.save(op)
    result = _execute(p)
    assert result["status"] == "needs_review" and world.sent == []
    assert [r["state"] for r in operations.load(p["operation_token"])["rows"]] == [
        "unknown",
        "pending",
        "pending",
    ]
