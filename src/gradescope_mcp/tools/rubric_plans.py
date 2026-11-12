"""Export, copy and reconcile flat rubric entries through approved plans."""

import json

from gradescope_mcp import operations as ops
from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import grading_ops as grading
from gradescope_mcp.tools.markup_check import markup_warnings_for
from gradescope_mcp.tools.safety import question_write_lock, write_confirmation_required

# Row states. "sending" (interrupted mid-request), "unknown" (sent but not
# verified), "stale" (rubric differed before sending) and "needs_review"
# (resume could not reconcile) are never replayed blindly.
_FAILED_STATES = ("unknown", "stale", "needs_review")
_PENDING_STATES = ("pending", "sending")


def _rubric(c, q):
    ctx = grading._load_question_rubric_context(c, q)
    items = grading._resolve_rubric(ctx.get("props"))
    if ctx.get("props") is None:
        # The existing creator supports empty rubrics, but a bulk replacement
        # cannot safely infer emptiness from a missing component.
        raise ValueError(
            "Cannot establish the current rubric; use an accessible grading page"
        )
    return [
        {
            "id": str(r["id"]),
            "description": r.get("description", ""),
            "weight": float(r["weight"]),
        }
        for r in items
    ]


def export_question_rubric(course_id: str, question_id: str) -> str:
    try:
        items = _rubric(course_id, question_id)
        return json.dumps(
            {
                "course_id": course_id,
                "question_id": question_id,
                "revision": ops.digest(items),
                "items": items,
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def prepare_rubric_plan(
    course_id: str, question_id: str, items: list[dict], remove_missing: bool = False
) -> str:
    try:
        if (
            type(remove_missing) is not bool
            or not isinstance(items, list)
            or len(items) > 200
        ):
            raise ValueError("Invalid rubric list or remove_missing")
        current = _rubric(course_id, question_id)
        by_id = {r["id"]: r for r in current}
        desired = []
        seen = set()
        for item in items:
            if not isinstance(item, dict) or set(item) - {
                "id",
                "description",
                "weight",
            }:
                raise ValueError("Items accept only id, description, weight")
            if not isinstance(item.get("description"), str):
                raise ValueError("Description must be text")
            weight = grading._validate_rubric_weight(item.get("weight"), True)
            sid = str(item["id"]) if item.get("id") is not None else None
            if sid is not None and (sid not in by_id or sid in seen):
                raise ValueError("Unknown or duplicate rubric ID")
            if sid:
                seen.add(sid)
            desired.append(
                {"id": sid, "description": item["description"], "weight": weight}
            )
        rows = []
        for item in desired:
            if item["id"] is None:
                rows.append({"action": "create", "item": item, "state": "pending"})
            elif item != by_id[item["id"]]:
                rows.append({"action": "update", "item": item, "state": "pending"})
        if remove_missing:
            rows += [
                {"action": "delete", "item": r, "state": "pending"}
                for r in current
                if r["id"] not in seen
            ]
        req = {
            "course_id": course_id,
            "question_id": question_id,
            "items": desired,
            "remove_missing": remove_missing,
            "actions": [{"action": r["action"], "item": r["item"]} for r in rows],
        }
        op = ops.create("rubric", req, current, rows)
        _save(op)
        # Advisory only: a lone "$" may be a currency sign, so never block.
        markup = markup_warnings_for(
            {f"items[{i}].description": item["description"] for i, item in enumerate(desired)}
        )
        notice = "No writes. Show every change and obtain approval. Updates/deletions can change all existing grades. Copy items to another question by omitting their IDs."
        if markup:
            notice += " Some descriptions have suspicious math markup (see markup_warnings); fix them before approval if they are not intended."
        return json.dumps(
            {
                "operation_token": op["operation_token"],
                "request_digest": op["request_digest"],
                "current": current,
                "actions": rows,
                "markup_warnings": markup,
                "notice": notice,
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def execute_rubric_plan(
    operation_token: str, request_digest: str, confirm_write: bool = False
) -> str:
    try:
        initial = ops.load(operation_token)
        req = initial["request"]
        if initial["kind"] != "rubric" or request_digest != initial["request_digest"]:
            raise ValueError("Plan kind/digest mismatch")
        if not confirm_write:
            return write_confirmation_required(
                "execute_rubric_plan",
                [
                    json.dumps(initial["rows"]),
                    f"operation_token={operation_token}",
                    f"request_digest={request_digest}",
                ],
            )
        c, q = req["course_id"], req["question_id"]
        with question_write_lock(c, q):
            op = ops.load(operation_token)
            if op["status"] not in ("prepared", "completed"):
                return (
                    "Error: This rubric operation was already attempted; inspect "
                    "its journal (tool_get_operation) or call "
                    "tool_resume_rubric_plan, which reconciles uncertain rows "
                    "against the live rubric before continuing."
                )
            if op["status"] == "completed":
                return json.dumps(op)
            if _keyed(_rubric(c, q)) != _keyed(op["snapshots"]):
                raise ValueError("Rubric changed since preview")
            return _run_rows(op, c, q, _keyed(op["snapshots"]))
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def resume_rubric_plan(
    operation_token: str, request_digest: str, confirm_write: bool = False
) -> str:
    """Continue an interrupted or stopped rubric plan without duplicating writes.

    Verified rows are skipped. Each row whose outcome is uncertain is first
    reconciled against the live rubric: an applied create/update/delete is
    marked verified (a create records the found item's ID and is never sent
    again), an unapplied one is sent under the same pre-send freshness check
    and post-write verification as execution, and anything ambiguous (e.g.
    two matching new items, or unrelated changes) stops as needs_review.
    """
    try:
        initial = ops.load(operation_token)
        req = initial["request"]
        if initial["kind"] != "rubric" or request_digest != initial["request_digest"]:
            raise ValueError("Plan kind/digest mismatch")
        if ops.cancelled(operation_token):
            raise ValueError(
                "This rubric operation was cancelled; prepare a new plan for "
                "the remaining changes"
            )
        if not confirm_write:
            open_rows = [
                {"index": i, "action": r["action"], "state": r.get("state"), "item": r["item"]}
                for i, r in enumerate(initial["rows"])
                if r.get("state") != "verified"
            ]
            return write_confirmation_required(
                "resume_rubric_plan",
                [
                    f"status={initial['status']}",
                    f"progress={json.dumps(_progress(initial))}",
                    "rows to reconcile or send: " + json.dumps(open_rows),
                    f"operation_token={operation_token}",
                    f"request_digest={request_digest}",
                ],
            )
        c, q = req["course_id"], req["question_id"]
        with question_write_lock(c, q):
            op = ops.load(operation_token)
            if op["status"] == "completed":
                return json.dumps(op)
            if op["status"] == "prepared":
                if _keyed(_rubric(c, q)) != _keyed(op["snapshots"]):
                    raise ValueError("Rubric changed since preview")
                return _run_rows(op, c, q, _keyed(op["snapshots"]))
            model = _keyed(op["snapshots"])
            rows = op["rows"]
            start = 0
            while start < len(rows) and rows[start].get("state") == "verified":
                if not _apply(model, rows[start]):
                    return _stop(
                        op, rows[start], start,
                        "verified create has no recorded created_id",
                    )
                start += 1
            if any(r.get("state") == "verified" for r in rows[start:]):
                # Execution is strictly sequential; a verified row after an
                # unverified one means the journal is not one we wrote.
                raise ValueError("Journal rows are out of order; inspect manually")
            current = _keyed(_rubric(c, q))
            while start < len(rows) and rows[start].get("state") != "pending":
                row = rows[start]
                verdict, detail = _reconcile(row, model, current)
                row["reconciled"] = verdict
                if verdict == "review":
                    return _stop(op, row, start, detail)
                if verdict == "send":
                    break  # not applied; _run_rows sends it with full checks
                row["state"] = "verified"
                row.pop("error", None)
                if detail is not None:
                    row["created_id"] = detail
                _apply(model, row)
                _save(op, start)
                start += 1
            return _run_rows(op, c, q, model, start)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def _keyed(items):
    """Rubric state as {id: (description, weight)}; display order is ignored."""
    return {r["id"]: (r["description"], r["weight"]) for r in items}


def _progress(op, current_index=None):
    rows = op.get("rows", [])
    states = [r.get("state") for r in rows]
    return {
        "total": len(rows),
        "verified": states.count("verified"),
        "failed": sum(states.count(s) for s in _FAILED_STATES),
        "pending": sum(states.count(s) for s in _PENDING_STATES),
        "current_index": current_index,
    }


def _save(op, current_index=None):
    op["progress"] = _progress(op, current_index)
    ops.save(op)


def _stop(op, row, index, reason):
    row["state"] = "needs_review"
    row["review_reason"] = reason
    op["status"] = "needs_review"
    _save(op, index)
    return json.dumps(op)


def _apply(model, row) -> bool:
    """Apply a verified row's effect to the expected rubric model."""
    item = row["item"]
    if row["action"] == "create":
        if not row.get("created_id"):
            return False
        model[row["created_id"]] = (item["description"], item["weight"])
    elif row["action"] == "update":
        model[item["id"]] = (item["description"], item["weight"])
    else:
        model.pop(item["id"], None)
    return True


def _reconcile(row, model, current):
    """Decide an uncertain row from the live rubric.

    Returns ("verified", created_id_or_None), ("send", None) when the row was
    provably not applied and everything else matches the plan's expectation,
    or ("review", reason).
    """
    item = row["item"]
    want = (item["description"], item["weight"])
    if row["action"] == "create":
        if any(current.get(k) != v for k, v in model.items()):
            return "review", "rubric items changed outside this plan"
        extra = {k: v for k, v in current.items() if k not in model}
        matches = [k for k, v in extra.items() if v == want]
        if len(matches) != len(extra):
            return "review", "rubric has new items this plan did not create"
        if len(matches) > 1:
            return "review", f"{len(matches)} matching new items; cannot tell which one this plan created"
        if matches:
            return "verified", matches[0]
        return "send", None
    target = item["id"]
    rest_model = {k: v for k, v in model.items() if k != target}
    rest_current = {k: v for k, v in current.items() if k != target}
    if rest_model != rest_current:
        return "review", "rubric changed outside this plan"
    if row["action"] == "update":
        if current.get(target) == want:
            return "verified", None
        if target in current and current[target] == model.get(target):
            return "send", None
        return "review", "target item has neither the old nor the planned values"
    if target not in current:
        return "verified", None
    if current[target] == model.get(target):
        return "send", None
    return "review", "item to delete changed since the plan"


def _send(c, q, row):
    item = row["item"]
    if row["action"] == "create":
        return grading.create_rubric_item(
            c, q, item["description"], item["weight"], True, True
        )
    if row["action"] == "update":
        return grading.update_rubric_item(
            c,
            q,
            item["id"],
            description=item["description"],
            weight=item["weight"],
            confirm_write=True,
            allow_negative=True,
        )
    return grading.delete_rubric_item(c, q, item["id"], True)


def _run_rows(op, c, q, expected, start=0):
    """Send rows[start:] one at a time; the caller holds the question lock.

    ``expected`` is the rubric state the next row must find; each row is
    re-checked against it before sending and verified by a re-read after.
    """
    token = op["operation_token"]
    for index in range(start, len(op["rows"])):
        row = op["rows"][index]
        if ops.cancelled(token):
            op["status"] = "cancelled"
            _save(op, index)
            return json.dumps(op)
        item = row["item"]
        row["state"] = "sending"
        op["status"] = "running"
        _save(op, index)
        before = _rubric(c, q)
        if _keyed(before) != expected:
            row["state"] = "stale"
            op["status"] = "needs_review"
            _save(op, index)
            return json.dumps(op)
        try:
            row["result"] = _send(c, q, row)
            after = _rubric(c, q)
            if row["action"] == "create":
                old_ids = {r["id"] for r in before}
                new = [r for r in after if r["id"] not in old_ids]
                good = len(new) == 1 and all(
                    new[0][k] == item[k] for k in ("description", "weight")
                )
                if good:
                    row["created_id"] = new[0]["id"]
            elif row["action"] == "update":
                good = any(r == item for r in after)
            else:
                good = all(r["id"] != item["id"] for r in after)
            row["state"] = "verified" if good else "unknown"
            if good:
                row.pop("error", None)
                expected = _keyed(after)
        except Exception as exc:
            row["state"] = "unknown"
            row["error"] = str(exc)
        _save(op, index)
        if row["state"] != "verified":
            op["status"] = "needs_review"
            _save(op, index)
            return json.dumps(op)
    op["status"] = "completed"
    _save(op)
    return json.dumps(op)
