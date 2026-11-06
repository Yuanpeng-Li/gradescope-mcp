"""Assignment readiness, score auditing, evidence archives and class-level plans."""

import io
import json
import math
import random
import zipfile

from gradescope_mcp import operations as ops
from gradescope_mcp.auth import AuthError
from gradescope_mcp.cache import get_artifact_path, write_artifact
from gradescope_mcp.tools.grading import (
    _get_outline_data,
    _fetch_assignment_scores_csv,
    _NON_QUESTION_CSV_COLUMNS,
)
from gradescope_mcp.tools.grading_workflow import _fetch_assignment_questions
from gradescope_mcp.tools.grading_ops import (
    _fetch_question_submission_entries,
    _load_question_rubric_context,
)
from gradescope_mcp.tools.assignment_admin import get_assignment_admin
from gradescope_mcp.tools.rubric_plans import _rubric
from gradescope_mcp.tools.question_settings import _scoring
from gradescope_mcp.tools.answer_groups import _fetch_answer_groups_json
from gradescope_mcp.tools.grading_plans import prepare_grade_plan


def _finite(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _outline_snapshot(props):
    return {
        "questions": props.get("questions"),
        "outline": props.get("outline"),
        "assignment": {
            k: (props.get("assignment") or {}).get(k) for k in ("id", "title", "type")
        },
    }


def review_assignment(
    course_id: str, assignment_id: str, include_rubrics: bool = False
) -> str:
    try:
        qs = _fetch_assignment_questions(course_id, assignment_id)
        rows, fields = _fetch_assignment_scores_csv(course_id, assignment_id)
        parents = {
            str(q.get("parent_id"))
            for q in qs.values()
            if q.get("parent_id") is not None
        }
        leaves = {
            qid: q
            for qid, q in qs.items()
            if qid not in parents
            and not q.get("question_group")
            and q.get("type") != "QuestionGroup"
        }
        questions = []
        issues = []
        unknown = []
        for qid, q in leaves.items():
            graded, total = (
                _finite(q.get("total_graded_count")),
                _finite(q.get("total_count")),
            )
            item = {
                "question_id": qid,
                "weight": q.get("weight"),
                "graded": graded,
                "total": total,
            }
            if graded is None or total is None:
                unknown.append(f"Question {qid}: progress unavailable")
            elif graded < total:
                issues.append(
                    {
                        "kind": "ungraded",
                        "question_id": qid,
                        "remaining": total - graded,
                    }
                )
            if include_rubrics:
                try:
                    ctx = _load_question_rubric_context(course_id, qid)
                    p = ctx.get("props") or {}
                    question = p.get("question") or {}
                    items = _rubric(course_id, qid)
                    item["rubric_items"] = len(items)
                    item["basis_revision"] = ops.digest(
                        {"question": question, "rubric": items}
                    )
                    if not items:
                        issues.append({"kind": "empty_rubric", "question_id": qid})
                    if _finite(question.get("weight")) == 0 and (
                        question.get("scoring_type") != "positive"
                        or question.get("ceiling") is not False
                    ):
                        issues.append(
                            {"kind": "zero_point_bonus_blocked", "question_id": qid}
                        )
                except AuthError:
                    raise
                except Exception:
                    unknown.append(f"Question {qid}: rubric unavailable")
            questions.append(item)
        question_cols = [f for f in fields if f not in _NON_QUESTION_CSV_COLUMNS]
        for row in rows:
            sid = str(row.get("Submission ID") or "")
            if row.get("Status") == "Missing":
                issues.append({"kind": "missing_submission", "submission_id": sid})
                continue
            total = _finite(row.get("Total Score"))
            maximum = _finite(row.get("Max Points"))
            if row.get("Status") == "Graded" and total is None:
                issues.append({"kind": "invalid_total", "submission_id": sid})
            if total is not None and (
                total < 0 or maximum is not None and total > maximum
            ):
                issues.append(
                    {
                        "kind": "score_outside_nominal_range",
                        "submission_id": sid,
                        "score": total,
                        "maximum": maximum,
                        "note": "May be intentional bonus/penalty; requires review",
                    }
                )
            scores = [_finite(row.get(f)) for f in question_cols]
            if (
                row.get("Status") == "Graded"
                and scores
                and all(v is not None for v in scores)
                and total is not None
                and abs(sum(scores) - total) > 0.01
            ):
                issues.append(
                    {
                        "kind": "total_mismatch",
                        "submission_id": sid,
                        "sum": sum(scores),
                        "total": total,
                    }
                )
        raw = get_assignment_admin(course_id, assignment_id)
        admin = (
            json.loads(raw)
            if not raw.startswith(("Error:", "Authentication error:"))
            else {"unknown": raw}
        )
        return json.dumps(
            {
                "course_id": course_id,
                "assignment_id": assignment_id,
                "questions": questions,
                "publication": admin.get("published"),
                "capabilities": admin.get("capabilities"),
                "issues": issues,
                "unknown": unknown,
                "all_reported_submissions_graded": all(
                    q["graded"] is not None
                    and q["total"] is not None
                    and q["graded"] == q["total"]
                    for q in questions
                )
                if questions
                else False,
                "notice": "Mechanical checks only; does not certify grading correctness or authorize publication.",
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def sample_question_submissions(
    course_id: str,
    question_id: str,
    count: int = 10,
    seed: int = 0,
    only_ungraded: bool = True,
) -> str:
    try:
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("Sample count must be 1–100")
        entries = _fetch_question_submission_entries(course_id, question_id)
        eligible = [e for e in entries if not only_ungraded or e.get("graded") is False]
        selected = random.Random(seed).sample(
            sorted(eligible, key=lambda e: str(e["submission_id"])),
            min(count, len(eligible)),
        )
        return json.dumps(
            {
                "population": len(eligible),
                "seed": seed,
                "submissions": [
                    {"submission_id": e["submission_id"], "graded": e.get("graded")}
                    for e in selected
                ],
                "notice": "Sample IDs only; read each actual answer before proposing grades.",
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def archive_assignment(
    course_id: str, assignment_id: str, include_rubrics: bool = True
) -> str:
    try:
        outline = _outline_snapshot(_get_outline_data(course_id, assignment_id))
        rows, fields = _fetch_assignment_scores_csv(course_id, assignment_id)
        qs = outline.get("questions") or {}
        parents = {
            str(q.get("parent_id"))
            for q in qs.values()
            if q.get("parent_id") is not None
        }
        rubrics = {}
        scoring = {}
        errors = {}
        if include_rubrics:
            for qid in qs:
                if qid in parents:
                    continue
                try:
                    rubrics[qid] = _rubric(course_id, qid)
                    scoring[qid] = _scoring(course_id, qid)[1]
                except AuthError:
                    raise
                except Exception as exc:
                    errors[qid] = str(exc)
        payload = {
            "outline": outline,
            "scores": rows,
            "fields": fields,
            "rubrics": rubrics,
            "scoring": scoring,
        }
        metadata = {
            "course_id": course_id,
            "assignment_id": assignment_id,
            "created_at": ops.timestamp(),
            "outline_revision": ops.digest(outline),
            "rubric_revisions": {qid: ops.digest(r) for qid, r in rubrics.items()},
            "scoring_revisions": {qid: ops.digest(q) for qid, q in scoring.items()},
            "rubrics_checked": include_rubrics,
            "errors": errors,
            "complete": not errors,
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
            for name, value in {**payload, "manifest": metadata}.items():
                z.writestr(name + ".json", json.dumps(value, ensure_ascii=False))
        token = ops.create("archive", metadata, {}, [])["operation_token"]
        path = write_artifact(
            get_artifact_path(f"assignment-archive-{assignment_id}-{token}.zip"),
            output.getvalue(),
        )
        return json.dumps(
            {
                "operation_token": token,
                "path": str(path),
                "manifest": metadata,
                "notice": "Private archive; includes student scores. It is a snapshot, not a restore command.",
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def check_archive_freshness(operation_token: str) -> str:
    try:
        record = ops.load(operation_token)
        if record["kind"] != "archive":
            raise ValueError("Not an archive operation")
        r = record["request"]
        c, a = r["course_id"], r["assignment_id"]
        changes = []
        unknown = []
        if (
            ops.digest(_outline_snapshot(_get_outline_data(c, a)))
            != r["outline_revision"]
        ):
            changes.append("outline")
        if not r.get("rubrics_checked") or "scoring_revisions" not in r:
            unknown.append("rubric/scoring coverage")
        for qid, revision in r.get("scoring_revisions", {}).items():
            try:
                if ops.digest(_scoring(c, qid)[1]) != revision:
                    changes.append("scoring:" + qid)
            except AuthError:
                raise
            except Exception:
                unknown.append("scoring:" + qid)
        for qid, revision in r["rubric_revisions"].items():
            try:
                if ops.digest(_rubric(c, qid)) != revision:
                    changes.append("rubric:" + qid)
            except AuthError:
                raise
            except Exception:
                unknown.append("rubric:" + qid)
        return json.dumps(
            {
                "state": "stale"
                if changes
                else "unknown"
                if unknown or r["errors"]
                else "fresh",
                "changed": changes,
                "unknown": unknown,
                "notice": "Checks outline/rubric only; archived student scores are historical.",
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def prepare_choice_bonus_plan(
    course_id: str,
    question_id: str,
    answers: dict[str, str],
    points_a: float = 2,
    points_b: float = 4,
    points_c: float = 1,
) -> str:
    try:
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            or v < 0
            for v in (points_a, points_b, points_c)
        ):
            raise ValueError("Bonus values must be finite and nonnegative")
        entries = _fetch_question_submission_entries(course_id, question_id)
        ids = {str(e["submission_id"]) for e in entries}
        if not ids or set(answers) != ids:
            raise ValueError(
                "Answers must cover exactly all listed question submissions; resolve omissions before planning"
            )
        if any(v not in ("A", "B", "C") for v in answers.values()):
            raise ValueError(
                "Only confirmed A/B/C choices accepted; resolve illegible/blank answers first"
            )
        q = (
            _load_question_rubric_context(course_id, question_id).get("props") or {}
        ).get("question") or {}
        if (
            _finite(q.get("weight")) != 0
            or q.get("scoring_type") != "positive"
            or q.get("ceiling") is not False
        ):
            raise ValueError("Requires positive scoring, zero weight, ceiling disabled")
        a = sum(v == "A" for v in answers.values())
        b = sum(v == "B" for v in answers.values())
        scores = {
            "A": points_a if a <= b else 0,
            "B": points_b if b <= a else 0,
            "C": points_c,
        }
        rows = [
            {
                "submission_id": sid,
                "rubric_item_ids": [],
                "point_adjustment": scores[choice],
                "comment": f"Choice {choice}; class counts A={a}, B={b}, C={len(ids) - a - b}. Minority rule; ties reward both A and B.",
            }
            for sid, choice in sorted(answers.items())
        ]
        result = prepare_grade_plan(course_id, question_id, rows)
        if result.startswith(("Error:", "Authentication error:")):
            return result
        p = json.loads(result)
        p["counts"] = {"A": a, "B": b, "C": len(ids) - a - b}
        p["choice_scores"] = scores
        p["notice"] = (
            "Human review of extracted choices and class counts required. No grades written. Executing this plan explicitly clears rubric selections and sets point adjustments."
        )
        return json.dumps(p)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"
