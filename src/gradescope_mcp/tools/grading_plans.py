"""Sample calibration and resumable, snapshot-bound grading plans.

Preparation reads every submission's grading page (concurrently; reads only)
and stores a private, digest-bound plan. Execution re-reads each submission
immediately before its write, refuses rows whose state changed since the
plan, posts the exact planned payload and verifies it by reading it back,
including the numeric score. Every row state change is journaled before the
next request, so ``resume_operation`` can tell a write that landed from one
that never did.
"""

import concurrent.futures as cf
import json
import logging
import math
import os
import threading

from gradescope_mcp import auth
from gradescope_mcp import operations as ops
from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import grading_ops as grading
from gradescope_mcp.tools.markup_check import math_markup_warnings
from gradescope_mcp.tools.safety import (
    question_write_lock,
    serialized_write,
    write_confirmation_required,
)

logger = logging.getLogger(__name__)


def _basis(props):
    q = props.get("question") or {}
    return {
        "question": {
            k: q.get(k) for k in ("id", "weight", "scoring_type", "floor", "ceiling")
        },
        "rubric": [
            {k: r.get(k) for k in ("id", "description", "weight")}
            for r in grading._resolve_rubric(props)
        ],
    }


def _state(props):
    return {
        "basis": _basis(props),
        "graded": grading._is_graded(props),
        "checked": sorted(grading._applied_rubric_ids(props)),
        "evaluation": props.get("evaluation"),
    }


# ---------------------------------------------------------------------------
# Concurrency settings
# ---------------------------------------------------------------------------

READ_CONCURRENCY_ENV = "GRADESCOPE_MCP_READ_CONCURRENCY"
WRITE_CONCURRENCY_ENV = "GRADESCOPE_MCP_WRITE_CONCURRENCY"
DEFAULT_READ_CONCURRENCY = 4
MAX_READ_CONCURRENCY = 8
# Rows are written one at a time unless the operator opts in: writes cannot
# be undone, Gradescope's tolerance for parallel save_grade calls has not
# been measured, and with one writer "stop at the first problem" is exact.
DEFAULT_WRITE_CONCURRENCY = 1
MAX_WRITE_CONCURRENCY = 4

SCORE_TOLERANCE = 1e-6
ATTENTION_LIMIT = 20
COMMENT_PREVIEW_CHARS = 160

# Row outcomes that may or may not have reached Gradescope (or did, but could
# not be confirmed); only resume_operation reconciles them.
UNCERTAIN_STATES = ("sending", "unknown", "accepted_unverified", "score_mismatch")
FAILED_STATES = ("stale", "unknown", "accepted_unverified", "score_mismatch")
# Per-attempt detail cleared when a row changes state, so an old error never
# sits next to a newer outcome.
_TRANSIENT_KEYS = (
    "error",
    "http_status",
    "verification",
    "verification_note",
    "actual_score",
    "stale_reason",
    "attention",
)


def _env_workers(name, default, maximum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", name, raw)
        return default
    return max(1, min(maximum, value))


def read_concurrency():
    """Worker threads for independent grading-page reads (1–8, default 4)."""
    return _env_workers(
        READ_CONCURRENCY_ENV, DEFAULT_READ_CONCURRENCY, MAX_READ_CONCURRENCY
    )


def write_concurrency():
    """Rows written side by side (1–4, default 1)."""
    return _env_workers(
        WRITE_CONCURRENCY_ENV, DEFAULT_WRITE_CONCURRENCY, MAX_WRITE_CONCURRENCY
    )


def _concurrent_map(fn, items, workers):
    """``[fn(x) for x in items]`` on up to ``workers`` threads, in input order.

    For read-only work. Each item runs with its own auth call state, which
    is merged back into the calling thread (``auth.merge_call_state``) so
    ``with_session_recovery`` still sees expiries and accepted writes. After
    the first failure no new item starts; the error raised is that of the
    lowest-index failing item, which is the error a sequential loop would
    have raised (items start in order, so every earlier item has run).
    """
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [fn(item) for item in items]
    stop = threading.Event()

    def task(item):
        if stop.is_set():
            return None
        outcome = auth.run_with_call_state(fn, item)
        if outcome[1] is not None:
            stop.set()
        return outcome

    results, errors = [None] * len(items), {}
    with cf.ThreadPoolExecutor(
        max_workers=min(workers, len(items)), thread_name_prefix="gradescope-read"
    ) as pool:
        futures = [pool.submit(task, item) for item in items]
        for index, future in enumerate(futures):
            outcome = future.result()
            if outcome is None:
                continue
            result, exc, state = outcome
            auth.merge_call_state(state)
            if exc is not None:
                errors[index] = exc
            else:
                results[index] = result
    if errors:
        raise errors[min(errors)]
    return results


# ---------------------------------------------------------------------------
# Compact views
# ---------------------------------------------------------------------------


def _preview_text(text):
    text = str(text or "")
    if len(text) <= COMMENT_PREVIEW_CHARS:
        return text
    return text[: COMMENT_PREVIEW_CHARS - 1] + "…"


def _plan_view(row):
    """One proposed grade, compactly, for the approval step."""
    payload = row.get("payload") or {}
    evaluation = payload.get("question_submission_evaluation") or {}
    comment = evaluation.get("comments")
    view = {
        "submission_id": row["submission_id"],
        "state": row["state"],
        "projected_score": row.get("projected_score"),
        "rubric_item_ids": [
            rid
            for rid, item in (payload.get("rubric_items") or {}).items()
            if (item or {}).get("score") == "true"
        ],
        "point_adjustment": evaluation.get("points"),
        "comment_preview": _preview_text(comment),
        "needs_review": bool(row.get("needs_review")),
    }
    if comment and len(str(comment)) > COMMENT_PREVIEW_CHARS:
        view["comment_chars"] = len(str(comment))
    if row.get("blocked_reason"):
        view["blocked_reason"] = row["blocked_reason"]
    if row.get("markup_warnings"):
        view["markup_warnings"] = row["markup_warnings"]
    return view


_ATTENTION_KEYS = (
    "submission_id",
    "state",
    "projected_score",
    "actual_score",
    "blocked_reason",
    "stale_reason",
    "verification_note",
    "attention",
    "http_status",
    "error",
    "resumed_from",
)


def _attention_view(row):
    view = {k: row[k] for k in _ATTENTION_KEYS if row.get(k) is not None}
    mismatches = (row.get("verification") or {}).get("mismatches")
    if mismatches:
        view["mismatches"] = mismatches
    if row.get("reconciled"):
        view["reconciled"] = True
    return view


def _attention_rows(rows):
    """Rows a human should look at: problems first, then blocked rows."""
    order = {s: 0 for s in FAILED_STATES}
    order.update(sending=1, blocked=2)
    flagged = [r for r in rows if r.get("state") in order]
    flagged.sort(key=lambda r: order[r["state"]])  # stable: keeps row order
    return flagged


def _state_counts(rows):
    counts = {}
    for row in rows:
        state = row.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
    return counts


_NEXT_STEPS = {
    "completed": "All rows are written and verified (the score read back equals the projection).",
    "completed_with_blocked_rows": "All writable rows are verified. Blocked rows (low confidence or already graded) were not written; handle them separately.",
    "needs_review": "Inspect attention_rows (tool_get_operation with include_details=True pages the full journal). tool_resume_operation re-reads uncertain rows (sending/unknown/accepted_unverified/score_mismatch) before sending anything: rows Gradescope already holds become verified, a row is re-sent only if Gradescope still shows the planned-from state, and stale rows are left for a human. It never overwrites a changed submission.",
    "cancelled": "Cancelled between rows; later rows were not written. Cancellation is permanent for this operation: prepare a new plan for the remaining rows.",
    "interrupted": "Stopped before finishing (see status_reason). Rows already processed are journaled; tool_resume_operation re-reads uncertain rows before continuing.",
    "running": "Running in the background. Poll tool_get_operation with this operation_token for progress; do not start it again.",
}


def _summary(op, verbose=False):
    rows = op.get("rows", [])
    if verbose:
        result = {k: v for k, v in op.items() if k not in ("request", "snapshots")}
    else:
        attention = _attention_rows(rows)
        result = {
            "operation_token": op["operation_token"],
            "request_digest": op["request_digest"],
            "status": op.get("status"),
            "progress": op.get("progress"),
            "row_counts": _state_counts(rows),
            "attention_rows": [
                _attention_view(r) for r in attention[:ATTENTION_LIMIT]
            ],
            "attention_omitted": max(0, len(attention) - ATTENTION_LIMIT),
        }
        if op.get("status_reason"):
            result["status_reason"] = op["status_reason"]
    result["next_step"] = _NEXT_STEPS.get(op.get("status"), "Query tool_get_operation.")
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


def _plan_row(course_id, question_id, row, evidence):
    """Read one submission and plan its grade (read-only; runs on a worker)."""
    sid = row["submission_id"]
    try:
        ctx = grading._get_grading_context(course_id, question_id, sid)
        props = ctx["props"]
        question = props.get("question") or {}
        if question.get("scoring_type") not in ("positive", "negative") or any(
            type(question.get(k)) is not bool for k in ("floor", "ceiling")
        ):
            raise ValueError(
                "Scoring mode, floor and ceiling must be known before planning"
            )
        problem = grading._write_target_problem(props, course_id, question_id, sid)
        if problem:
            raise ValueError(problem)
        plan = grading._plan_grade_write(
            props, row["rubric_item_ids"], row["point_adjustment"], row["comment"]
        )
    except AuthError:
        raise
    except Exception as exc:
        raise ValueError(f"submission {sid}: {exc}") from exc
    low = grading._is_low_confidence(row)
    blocked = low or (
        grading._is_graded(props)
        and not row["overwrite"]
        and not grading._already_holds(props, plan)
    )
    planned = {
        "submission_id": sid,
        "state": "blocked" if blocked else "pending",
        "payload": plan["payload"],
        "projected_score": plan["projected_score"],
        "needs_review": grading._needs_review(row["confidence"]),
        "evidence": evidence.get(sid),
        "blocked_reason": "low_confidence"
        if low
        else "already_graded"
        if blocked
        else None,
        # Advisory only: a lone "$" may be a currency sign.
        "markup_warnings": math_markup_warnings(plan["resolved_comments"]),
    }
    return _state(props), planned


def prepare_grade_plan(
    course_id: str,
    question_id: str,
    grades: list[dict],
    evidence: dict | None = None,
    verbose: bool = False,
) -> str:
    try:
        if not isinstance(grades, list) or not 1 <= len(grades) <= 500:
            return "Error: Supply 1–500 proposed grades; no writes made."
        evidence = evidence or {}
        if (
            not isinstance(evidence, dict)
            or len(json.dumps(evidence, allow_nan=False)) > 1024 * 1024
        ):
            raise ValueError("Evidence must be an object of at most 1 MiB")
        rows, errors = grading._normalize_batch_rows(grades)
        if errors:
            return "Error: " + "; ".join(errors)
        if set(evidence) - {r["submission_id"] for r in rows}:
            raise ValueError("Evidence must reference plan submission IDs")
        for row in rows:
            sid = row["submission_id"]
            if not sid.isascii() or not sid.isdigit():
                raise ValueError("Invalid question submission ID")
        # The grading pages are independent reads: fetch them concurrently.
        # Each page carries that submission's own evaluation, so it cannot be
        # replaced by one shared rubric read.
        results = _concurrent_map(
            lambda row: _plan_row(course_id, question_id, row, evidence),
            rows,
            read_concurrency(),
        )
        snapshots = {p["submission_id"]: snap for snap, p in results}
        planned = [p for _snap, p in results]
        request = {
            "course_id": course_id,
            "question_id": question_id,
            "grades": rows,
            "evidence": evidence,
            "payloads": [r["payload"] for r in planned],
        }
        operation = ops.create("grade_plan", request, snapshots, planned)
        counts = _state_counts(planned)
        warned = sum(1 for r in planned if r["markup_warnings"])
        notice = (
            "Local plan only. Show every proposed grade and obtain approval "
            "before execute_grade_plan."
        )
        if not verbose:
            notice += (
                " Comments are previewed; pass verbose=True for the exact "
                "payloads and evidence."
            )
        if warned:
            notice += (
                " markup_warnings are advisory Markdown/KaTeX checks of the "
                "comment text (e.g. math wrapped in $$ twice); fix the text and "
                "prepare again if they are real."
            )
        result = {
            "operation_token": operation["operation_token"],
            "request_digest": operation["request_digest"],
            "status": "prepared",
            "summary": {
                "rows": len(planned),
                "pending": counts.get("pending", 0),
                "blocked": counts.get("blocked", 0),
                "needs_review": sum(1 for r in planned if r["needs_review"]),
                "markup_warning_rows": warned,
            },
            "rows": planned if verbose else [_plan_view(r) for r in planned],
            "notice": notice,
        }
        return json.dumps(result, ensure_ascii=False)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: Cannot prepare grade plan: {exc}"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _score_outcome(actual, projected):
    """``("verified", extra)`` when Gradescope's score equals the projection."""
    if projected is None:
        return "accepted_unverified", {
            "verification_note": "the plan has no projected score to compare with"
        }
    value = grading._to_float(actual)
    if value is None:
        return "accepted_unverified", {
            "verification_note": "Gradescope did not report a numeric score"
        }
    if abs(value - float(projected)) > SCORE_TOLERANCE:
        return "score_mismatch", {
            "actual_score": value,
            "verification_note": (
                f"Gradescope shows {value:g}, the plan projected {float(projected):g}"
            ),
        }
    return "verified", {"actual_score": value}


def _verification_outcome(back, projected):
    """Row state after a save, from ``grading._read_back_grade``'s result."""
    if back.get("error"):
        return "accepted_unverified", {
            "verification_note": f"read-back failed: {back['error']}"
        }
    if back.get("mismatches"):
        return "accepted_unverified", {
            "verification_note": "the grade read back differs from what was sent"
        }
    if back.get("graded") is not True:
        return "accepted_unverified", {
            "verification_note": "Gradescope does not show the submission as graded"
        }
    return _score_outcome(back.get("score"), projected)


class _Run:
    """One execution pass over a plan; serializes journal updates."""

    def __init__(self, op, resume):
        self.op = op
        self.token = op["operation_token"]
        self.resume = resume
        self.lock = threading.RLock()
        self.halt_status = None
        self.halt_reason = None
        self.counters = {"checked": 0, "written": 0, "skipped": 0}
        self.current = (None, None)
        self.started_at = ops.timestamp()

    @property
    def halted(self):
        return self.halt_status is not None

    def halt(self, status, reason):
        """Stop dispatching new rows; the first reason wins."""
        with self.lock:
            if self.halt_status is None:
                self.halt_status, self.halt_reason = status, reason

    def count(self, key):
        with self.lock:
            self.counters[key] += 1

    def mark_current(self, index, sid):
        with self.lock:
            self.current = (index, sid)

    def progress(self):
        rows = self.op["rows"]
        counts = _state_counts(rows)
        return {
            "mode": "resume" if self.resume else "execute",
            "total": len(rows),
            **self.counters,
            "verified": counts.get("verified", 0),
            "failed": sum(counts.get(s, 0) for s in FAILED_STATES),
            "blocked": counts.get("blocked", 0),
            "in_flight": counts.get("sending", 0),
            "pending": counts.get("pending", 0),
            "current_index": self.current[0],
            "current_submission_id": self.current[1],
            "write_concurrency": write_concurrency(),
            "started_at": self.started_at,
            "updated_at": ops.timestamp(),
        }

    def save(self):
        with self.lock:
            self.op["progress"] = self.progress()
            ops.save(self.op)

    def set(self, row, state, **extra):
        with self.lock:
            for key in _TRANSIENT_KEYS:
                row.pop(key, None)
            row["state"] = state
            row.update({k: v for k, v in extra.items() if v is not None})
            self.save()

    def fail(self, row, state, reason, **extra):
        self.set(row, state, **extra)
        self.halt("needs_review", f"submission {row['submission_id']}: {reason}")

    def finish(self, status, reason=None):
        with self.lock:
            self.op["status"] = status
            if reason:
                self.op["status_reason"] = reason
            else:
                self.op.pop("status_reason", None)
            self.save()


def _process_row(run, index):
    """Check, write and verify one row. An exception stops the whole run."""
    op = run.op
    req = op["request"]
    c, q = req["course_id"], req["question_id"]
    row, original = op["rows"][index], req["grades"][index]
    sid = row["submission_id"]
    if ops.cancelled(run.token):
        run.halt("cancelled", "cancellation requested")
        return
    run.mark_current(index, sid)
    # Fresh read immediately before the write; never a prefetched one.
    ctx = grading._get_grading_context(c, q, sid)
    props = ctx["props"]
    problem = grading._write_target_problem(props, c, q, sid)
    if problem:
        raise ValueError(problem)
    run.count("checked")
    snapshot = op["snapshots"][sid]
    if ops.digest(_basis(props)) != ops.digest(snapshot["basis"]):
        reason = "the question's scoring or rubric changed since the plan was prepared"
        run.fail(row, "stale", reason, stale_reason=reason)
        return
    try:
        plan = grading._plan_grade_write(
            props,
            original["rubric_item_ids"],
            original["point_adjustment"],
            original["comment"],
        )
    except ValueError as exc:
        run.fail(row, "stale", str(exc), stale_reason=str(exc))
        return
    same_payload = plan["payload"] == row["payload"]
    prior = row["state"]
    # Resume re-reads rows flagged by an earlier run. Their outcome is final
    # for this run without sending anything, so it does not stop the other
    # rows; the run still ends as needs_review if one stays unverified.
    reconciling = run.resume and prior in UNCERTAIN_STATES
    if same_payload and grading._already_holds(props, plan):
        # Gradescope already holds the planned grade (an earlier attempt
        # landed, or someone entered it): check the score, send nothing.
        # Same rule as a read-back: the raw graded flag must be True (not
        # inferred from a present score) and the score must match.
        submission = props.get("submission") or {}
        state, extra = _verification_outcome(
            {
                "graded": submission.get("graded"),
                "score": submission.get("score"),
                "mismatches": [],
            },
            row.get("projected_score"),
        )
        run.set(row, state, reconciled=True, **extra)
        if state != "verified" and not reconciling:
            run.halt("needs_review", f"submission {sid}: {extra['verification_note']}")
        return
    resumed_from = None
    if prior in UNCERTAIN_STATES:
        if not run.resume:
            with run.lock:  # keep the earlier outcome's details
                row["attention"] = (
                    f"earlier outcome {prior} is uncertain; use "
                    "tool_resume_operation to reconcile it"
                )
                run.save()
            run.halt(
                "needs_review",
                f"submission {sid} has an uncertain earlier outcome ({prior}); "
                "execute never re-sends it",
            )
            return
        if not same_payload or ops.digest(_state(props)) != ops.digest(snapshot):
            reason = (
                f"earlier outcome was {prior}, and Gradescope now holds neither "
                "the planned grade nor the state the plan was made from; review by hand"
            )
            run.set(row, "stale", stale_reason=reason, resumed_from=prior)
            return
        # Gradescope still shows exactly the planned-from state: the earlier
        # write never landed, so this send is the first effective write.
        resumed_from = prior
    elif not same_payload or ops.digest(_state(props)) != ops.digest(snapshot):
        reason = "the submission's grade changed since the plan was prepared"
        run.fail(row, "stale", reason, stale_reason=reason)
        return
    if ops.cancelled(run.token):
        run.halt("cancelled", "cancellation requested")
        return
    # Journal "sending" before the request: a crash after this point leaves
    # an uncertain row that resume re-reads instead of blindly re-sending.
    run.set(row, "sending", resumed_from=resumed_from)
    run.count("written")
    try:
        resp = grading._post_save_grade(ctx, row["payload"])
        if resp.status_code != 200:
            run.set(
                row, "unknown", http_status=resp.status_code, resumed_from=resumed_from
            )
        else:
            run.set(row, "accepted_unverified", resumed_from=resumed_from)
            back = grading._read_back_grade(c, q, sid, plan)
            state, extra = _verification_outcome(back, row.get("projected_score"))
            run.set(row, state, verification=back, resumed_from=resumed_from, **extra)
    except Exception as exc:
        run.set(
            row, "unknown", error=str(exc) or type(exc).__name__, resumed_from=resumed_from
        )
        if isinstance(exc, AuthError):
            run.halt(
                "interrupted",
                f"authentication failed while saving submission {sid}: {exc}",
            )
    if row["state"] != "verified":
        run.halt(
            "needs_review",
            f"submission {sid}: "
            f"{row.get('verification_note') or row.get('error') or row['state']}",
        )


def _dispatch(run):
    rows = run.op["rows"]
    skip = ("verified", "blocked") + (("stale",) if run.resume else ())
    todo = [i for i, row in enumerate(rows) if row["state"] not in skip]
    run.counters["skipped"] = len(rows) - len(todo)
    # Rows with uncertain earlier outcomes go first, one at a time: execute
    # must stop on them before any other row is written, and resume settles
    # them before new writes start.
    uncertain = [i for i in todo if rows[i]["state"] in UNCERTAIN_STATES]
    for index in uncertain:
        if run.halted:
            return
        _process_row(run, index)
    todo = [i for i in todo if i not in set(uncertain)]
    workers = write_concurrency()
    if workers <= 1:
        for index in todo:
            if run.halted:
                break
            _process_row(run, index)
        return
    # Whole rows (check -> write -> verify, each for its own submission) run
    # side by side. No new row starts after a problem; rows already in flight
    # finish and are journaled.
    errors = {}
    remaining = iter(todo)
    with cf.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="gradescope-grade"
    ) as pool:
        inflight = {}
        while True:
            while len(inflight) < workers and not run.halted:
                index = next(remaining, None)
                if index is None:
                    break
                future = pool.submit(auth.run_with_call_state, _process_row, run, index)
                inflight[future] = index
            if not inflight:
                break
            done, _ = cf.wait(inflight, return_when=cf.FIRST_COMPLETED)
            for future in done:
                index = inflight.pop(future)
                _result, exc, state = future.result()
                auth.merge_call_state(state)
                if exc is not None:
                    errors[index] = exc
                    run.halt("interrupted", str(exc) or type(exc).__name__)
    if errors:
        raise errors[min(errors)]


def _completion_status(rows):
    states = {row["state"] for row in rows}
    if states & set(FAILED_STATES) or states & {"sending", "pending"}:
        return "needs_review"
    if "blocked" in states:
        return "completed_with_blocked_rows"
    return "completed"


def _execute_locked(token, resume):
    """Run the plan under its write locks; returns the finished ``_Run``."""
    req = ops.load(token)["request"]
    with serialized_write("operation", token):
        with question_write_lock(req["course_id"], req["question_id"]):
            # Reload under the cross-process lock, so two clients cannot
            # execute their separately loaded copies of the same plan.
            run = _Run(ops.load(token), resume)
            run.finish("running")
            try:
                _dispatch(run)
            except AuthError as exc:
                run.finish(
                    "interrupted",
                    f"Gradescope authentication failed or the session expired: {exc}",
                )
                raise
            except Exception as exc:
                run.finish("interrupted", str(exc) or type(exc).__name__)
                raise
            if run.halt_status:
                status, reason = run.halt_status, run.halt_reason
            else:
                status = _completion_status(run.op["rows"])
                counts = _state_counts(run.op["rows"])
                flagged = ", ".join(
                    f"{counts[s]} {s}" for s in FAILED_STATES if counts.get(s)
                )
                reason = (
                    f"rows need human review: {flagged}"
                    if status == "needs_review" and flagged
                    else None
                )
            run.finish(status, reason)
            return run


_background_threads: dict[str, threading.Thread] = {}


def _mark_interrupted(token, reason):
    try:
        op = ops.load(token)
        if op.get("status") == "running":
            op["status"] = "interrupted"
            op["status_reason"] = reason
            ops.save(op)
    except Exception:
        logger.exception("Cannot record the interruption of operation %s", token)


def _background_main(token, handle, resume):
    """Daemon-thread body. There is no session recovery here: an expiry ends
    the run as ``interrupted`` and resume_operation continues it."""
    try:
        _execute_locked(token, resume)
    except AuthError as exc:
        _mark_interrupted(
            token,
            f"Gradescope authentication failed or the session expired: {exc}. "
            "Use tool_resume_operation to continue.",
        )
    except Exception as exc:
        logger.warning(
            "Background grade plan %s stopped: %s", token, type(exc).__name__
        )
        _mark_interrupted(token, str(exc) or type(exc).__name__)
    finally:
        try:
            expired = auth.current_expired_connection()
            if expired is not None:
                # The next tool call logs in again instead of reusing it.
                auth.reset_connection(expired)
        finally:
            ops.release_run(handle)
            _background_threads.pop(token, None)


def _join_background(token, timeout=None):
    """Wait for a background run to end (tests and shutdown)."""
    thread = _background_threads.get(token)
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def _preview(action, op, resume, background):
    rows = op["rows"]
    skip = ("verified", "blocked") + (("stale",) if resume else ())
    todo = [r for r in rows if r["state"] not in skip]
    uncertain = [r for r in todo if r["state"] in UNCERTAIN_STATES]
    lines = [
        f"operation_token={op['operation_token']}",
        f"request_digest={op['request_digest']}",
        f"current status: {op.get('status')}; row states: "
        f"{json.dumps(_state_counts(rows))}",
        f"rows to re-check and write if unchanged: {len(todo) - len(uncertain)}",
    ]
    if resume:
        lines.append(
            f"uncertain rows to re-read first: {len(uncertain)} (verified if "
            "Gradescope already holds the planned grade and score; re-sent only "
            "if Gradescope still shows the planned-from state; otherwise stale)"
        )
        lines.append(
            "stale rows left for human review: "
            f"{sum(r['state'] == 'stale' for r in rows)}"
        )
    elif uncertain:
        lines.append(
            f"{len(uncertain)} row(s) have uncertain earlier outcomes; execute "
            "stops at them unless Gradescope already holds the planned grade. "
            "Use tool_resume_operation to reconcile them."
        )
    shown = todo[:ATTENTION_LIMIT]
    more = f" (+{len(todo) - len(shown)} more)" if len(todo) > len(shown) else ""
    lines.append(
        "rows: "
        + json.dumps([_plan_view(r) for r in shown], ensure_ascii=False)
        + more
    )
    lines.append(
        f"write concurrency: {write_concurrency()} row(s) at a time"
        + ("; runs in the background, poll tool_get_operation" if background else "")
    )
    return write_confirmation_required(action, lines)


def _start(action, token, request_digest, confirm_write, background, verbose, resume):
    try:
        op = ops.load(token)
        if op["kind"] != "grade_plan" or request_digest != op["request_digest"]:
            return "Error: Plan kind or request digest mismatch."
        if not confirm_write:
            return _preview(action, op, resume, background)
        try:
            handle = ops.acquire_run(token)
        except ops.OperationRunningError:
            return (
                f"Error: Operation {token} is already running (a background run "
                "or another execute/resume call holds it). Nothing was sent. Poll "
                "tool_get_operation for progress; do not start it again."
            )
        if background:
            try:
                # Reload under the run lock: the copy loaded above may predate
                # another run's journal updates and must not overwrite them.
                op = ops.load(token)
                if request_digest != op["request_digest"]:
                    raise ValueError("request digest mismatch")
                op["status"] = "running"
                op["status_reason"] = "started in the background"
                ops.save(op)
                thread = threading.Thread(
                    target=_background_main,
                    args=(token, handle, resume),
                    name=f"grade-plan-{token[:8]}",
                    daemon=True,
                )
                _background_threads[token] = thread
                thread.start()
            except BaseException:
                ops.release_run(handle)
                raise
            return json.dumps(
                {
                    "operation_token": token,
                    "request_digest": request_digest,
                    "status": "running",
                    "background": True,
                    "row_counts": _state_counts(op["rows"]),
                    "next_step": _NEXT_STEPS["running"],
                }
            )
        try:
            run = _execute_locked(token, resume)
        finally:
            ops.release_run(handle)
        return _summary(run.op, verbose)
    except AuthError as exc:
        return (
            f"Authentication error: {exc}. Rows already processed are journaled; "
            "query the operation, then use tool_resume_operation to continue."
        )
    except Exception as exc:
        return (
            f"Error: Cannot execute plan: {exc}. Query the operation before retrying."
        )


def execute_grade_plan(
    operation_token: str,
    request_digest: str,
    confirm_write: bool = False,
    background: bool = False,
    verbose: bool = False,
) -> str:
    """Execute an approved plan; stops at the first row that is not verified."""
    return _start(
        "execute_grade_plan",
        operation_token,
        request_digest,
        confirm_write,
        background,
        verbose,
        resume=False,
    )


def resume_operation(
    operation_token: str,
    request_digest: str,
    confirm_write: bool = False,
    background: bool = False,
    verbose: bool = False,
) -> str:
    """Continue a stopped grade plan, reconciling uncertain rows first."""
    try:
        kind = ops.load(operation_token)["kind"]
    except Exception as exc:
        return f"Error: Cannot read operation: {exc}"
    if kind != "grade_plan":
        return (
            "Error: resume_operation continues grade_plan operations only; "
            f"operation {operation_token} is a {kind!r} operation. Use that "
            "kind's own execute or resume tool. Nothing was sent."
        )
    return _start(
        "resume_operation",
        operation_token,
        request_digest,
        confirm_write,
        background,
        verbose,
        resume=True,
    )


def calibrate_grade_plan(
    operation_token: str, reference_scores: dict, tolerance: float = 0.5
) -> str:
    try:
        if isinstance(tolerance, bool) or not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("Invalid tolerance")
        op = ops.load(operation_token)
        if op["kind"] != "grade_plan":
            raise ValueError("Not a grade plan")
        rows = {r["submission_id"]: r for r in op["rows"]}
        if not reference_scores or any(str(s) not in rows for s in reference_scores):
            raise ValueError("References must identify plan rows")
        comparisons = []
        for sid, score in reference_scores.items():
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise ValueError("Reference scores must be finite numbers")
            projected = rows[str(sid)]["projected_score"]
            if projected is None:
                raise ValueError("Plan has unknown projected scores")
            comparisons.append(
                {
                    "submission_id": str(sid),
                    "reference": score,
                    "proposed": projected,
                    "difference": projected - score,
                    "within_tolerance": abs(projected - score) <= tolerance,
                }
            )
        return json.dumps(
            {
                "sample_size": len(comparisons),
                "mean_absolute_error": sum(abs(r["difference"]) for r in comparisons)
                / len(comparisons),
                "comparisons": comparisons,
                "notice": "Calibration only; no grades written. Sample agreement does not establish accuracy for unreviewed answers.",
            }
        )
    except Exception as exc:
        return f"Error: {exc}"
