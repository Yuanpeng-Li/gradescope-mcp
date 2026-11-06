"""Export, copy and reconcile flat rubric entries through approved plans."""

import json

from gradescope_mcp import operations as ops
from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import grading_ops as grading
from gradescope_mcp.tools.safety import question_write_lock, write_confirmation_required


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
        return json.dumps(
            {
                "operation_token": op["operation_token"],
                "request_digest": op["request_digest"],
                "current": current,
                "actions": rows,
                "notice": "No writes. Show every change and obtain approval. Updates/deletions can change all existing grades. Copy items to another question by omitting their IDs.",
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
                return "Error: This rubric operation was already attempted; inspect its journal before preparing remaining changes."
            if op["status"] == "completed":
                return json.dumps(op)
            if _rubric(c, q) != op["snapshots"]:
                raise ValueError("Rubric changed since preview")
            expected = op["snapshots"]
            for row in op["rows"]:
                if ops.cancelled(operation_token):
                    op["status"] = "cancelled"
                    ops.save(op)
                    return json.dumps(op)
                item = row["item"]
                row["state"] = "sending"
                op["status"] = "running"
                ops.save(op)
                before = _rubric(c, q)
                if before != expected:
                    row["state"] = "stale"
                    op["status"] = "needs_review"
                    ops.save(op)
                    return json.dumps(op)
                try:
                    if row["action"] == "create":
                        result = grading.create_rubric_item(
                            c, q, item["description"], item["weight"], True, True
                        )
                    elif row["action"] == "update":
                        result = grading.update_rubric_item(
                            c,
                            q,
                            item["id"],
                            description=item["description"],
                            weight=item["weight"],
                            confirm_write=True,
                            allow_negative=True,
                        )
                    else:
                        result = grading.delete_rubric_item(c, q, item["id"], True)
                    row["result"] = result
                    after = _rubric(c, q)
                    if row["action"] == "create":
                        new = [
                            r for r in after if r["id"] not in {r["id"] for r in before}
                        ]
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
                        expected = after
                except Exception as exc:
                    row["state"] = "unknown"
                    row["error"] = str(exc)
                ops.save(op)
                if row["state"] != "verified":
                    op["status"] = "needs_review"
                    ops.save(op)
                    return json.dumps(op)
            op["status"] = "completed"
            ops.save(op)
            return json.dumps(op)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"
