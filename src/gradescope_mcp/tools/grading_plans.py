"""Sample calibration and resumable, snapshot-bound grading plans."""

import json
import math

from gradescope_mcp import operations as ops
from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import grading_ops as grading
from gradescope_mcp.tools.safety import (
    question_write_lock,
    serialized_write,
    write_confirmation_required,
)


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


def prepare_grade_plan(
    course_id: str, question_id: str, grades: list[dict], evidence: dict | None = None
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
        snapshots, planned = {}, []
        for row in rows:
            sid = row["submission_id"]
            if not sid.isascii() or not sid.isdigit():
                raise ValueError("Invalid question submission ID")
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
            snapshots[sid] = _state(props)
            blocked = grading._is_low_confidence(row) or (
                grading._is_graded(props)
                and not row["overwrite"]
                and not grading._already_holds(props, plan)
            )
            planned.append(
                {
                    "submission_id": sid,
                    "state": "blocked" if blocked else "pending",
                    "payload": plan["payload"],
                    "projected_score": plan["projected_score"],
                    "needs_review": grading._needs_review(row["confidence"]),
                    "evidence": evidence.get(sid),
                    "blocked_reason": "low_confidence"
                    if grading._is_low_confidence(row)
                    else "already_graded"
                    if blocked
                    else None,
                }
            )
        request = {
            "course_id": course_id,
            "question_id": question_id,
            "grades": rows,
            "evidence": evidence,
            "payloads": [r["payload"] for r in planned],
        }
        operation = ops.create("grade_plan", request, snapshots, planned)
        return json.dumps(
            {
                "operation_token": operation["operation_token"],
                "request_digest": operation["request_digest"],
                "status": "prepared",
                "rows": planned,
                "notice": "Local plan only. Show every proposed grade and obtain approval before execute_grade_plan.",
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: Cannot prepare grade plan: {exc}"


def execute_grade_plan(
    operation_token: str, request_digest: str, confirm_write: bool = False
) -> str:
    try:
        with serialized_write("operation", operation_token):
            op = ops.load(operation_token)
            if op["kind"] != "grade_plan" or request_digest != op["request_digest"]:
                return "Error: Plan kind or request digest mismatch."
            if not confirm_write:
                return write_confirmation_required(
                    "execute_grade_plan",
                    [
                        json.dumps(op["rows"]),
                        f"operation_token={operation_token}",
                        f"request_digest={request_digest}",
                    ],
                )
            req = op["request"]
            c, q = req["course_id"], req["question_id"]
            with question_write_lock(c, q):
                # Reload under the cross-process lock, so two clients cannot
                # execute their separately loaded copies of the same plan.
                op = ops.load(operation_token)
                for row, original in zip(op["rows"], req["grades"]):
                    if row["state"] in ("verified", "blocked"):
                        continue
                    if ops.cancelled(operation_token):
                        op["status"] = "cancelled"
                        ops.save(op)
                        return json.dumps(op)
                    sid = row["submission_id"]
                    ctx = grading._get_grading_context(c, q, sid)
                    props = ctx["props"]
                    problem = grading._write_target_problem(props, c, q, sid)
                    if problem:
                        raise ValueError(problem)
                    plan = grading._plan_grade_write(
                        props,
                        original["rubric_item_ids"],
                        original["point_adjustment"],
                        original["comment"],
                    )
                    if ops.digest(_basis(props)) != ops.digest(
                        op["snapshots"][sid]["basis"]
                    ):
                        row["state"] = "stale"
                        op["status"] = "needs_review"
                        ops.save(op)
                        return json.dumps(op)
                    if (
                        grading._already_holds(props, plan)
                        and plan["payload"] == row["payload"]
                    ):
                        row["state"] = "verified"
                        row["reconciled"] = True
                        ops.save(op)
                        continue
                    if row["state"] in ("sending", "unknown", "accepted_unverified"):
                        op["status"] = "needs_review"
                        ops.save(op)
                        return json.dumps(op)
                    if (
                        ops.digest(_state(props)) != ops.digest(op["snapshots"][sid])
                        or plan["payload"] != row["payload"]
                    ):
                        row["state"] = "stale"
                        op["status"] = "needs_review"
                        ops.save(op)
                        return json.dumps(op)
                    if ops.cancelled(operation_token):
                        op["status"] = "cancelled"
                        ops.save(op)
                        return json.dumps(op)
                    row["state"] = "sending"
                    op["status"] = "running"
                    ops.save(op)
                    try:
                        resp = grading._post_save_grade(ctx, row["payload"])
                        if resp.status_code != 200:
                            row["state"] = "unknown"
                            row["http_status"] = resp.status_code
                        else:
                            row["state"] = "accepted_unverified"
                            ops.save(op)
                            back = grading._read_back_grade(c, q, sid, plan)
                            row["verification"] = back
                            row["state"] = (
                                "verified"
                                if not back.get("error")
                                and not back.get("mismatches")
                                and back.get("graded") is True
                                else "accepted_unverified"
                            )
                    except Exception as exc:
                        row["state"] = "unknown"
                        row["error"] = str(exc)
                    ops.save(op)
                    if row["state"] != "verified":
                        op["status"] = "needs_review"
                        ops.save(op)
                        return json.dumps(op)
                op["status"] = (
                    "completed_with_blocked_rows"
                    if any(r["state"] == "blocked" for r in op["rows"])
                    else "completed"
                )
                ops.save(op)
                return json.dumps(op)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return (
            f"Error: Cannot execute plan: {exc}. Query the operation before retrying."
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
