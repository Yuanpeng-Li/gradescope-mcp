"""Private, durable operation plans. Never infer retry safety from HTTP text alone."""

import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone

from gradescope_mcp.cache import get_artifact_path, write_artifact


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def path_for(token):
    if not isinstance(token, str) or not re.fullmatch("[a-f0-9]{32}", token):
        raise ValueError("Invalid operation token")
    return get_artifact_path(f"operation-{token}.json")


def save(plan):
    plan["updated_at"] = timestamp()
    write_artifact(
        path_for(plan["operation_token"]),
        json.dumps(plan, ensure_ascii=False, allow_nan=False),
    )


def load(token):
    fd = os.open(path_for(token), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
            raise ValueError("Invalid or oversized operation record")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            plan = json.load(handle)
    finally:
        os.close(fd)
    if plan.get("operation_token") != token or plan.get("version") != 1:
        raise ValueError("Invalid operation record")
    if digest(plan["request"]) != plan["request_digest"]:
        raise ValueError("Operation request changed; prepare a new plan")
    return plan


def create(kind, request, snapshots, rows):
    token = secrets.token_hex(16)
    plan = {
        "version": 1,
        "operation_token": token,
        "kind": kind,
        "created_at": timestamp(),
        "status": "prepared",
        "request": request,
        "request_digest": digest(request),
        "snapshots": snapshots,
        "rows": rows,
        "cancel_requested": False,
    }
    save(plan)
    return plan


def get_operation(
    operation_token: str,
    include_details: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> str:
    try:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("offset must be nonnegative and limit must be 1–100")
        plan = load(operation_token)
        rows = plan.get("rows", [])
        counts = {}
        for row in rows:
            state = row.get("state", "unknown")
            counts[state] = counts.get(state, 0) + 1
        selected = rows[offset : offset + limit]
        result = {
            k: v for k, v in plan.items() if k not in ("request", "snapshots", "rows")
        }
        result.update(
            row_counts=counts,
            total_rows=len(rows),
            offset=offset,
            next_offset=offset + limit if offset + limit < len(rows) else None,
            cancellation_requested=cancelled(operation_token),
        )
        result["rows"] = (
            selected
            if include_details
            else [
                {
                    k: v
                    for k, v in row.items()
                    if k not in ("payload", "evidence", "item", "result")
                }
                for row in selected
            ]
        )
        if include_details:
            result["request"] = {
                k: v
                for k, v in plan["request"].items()
                if k not in ("grades", "payloads", "evidence")
            }
            if plan["kind"] == "grade_plan":
                result["snapshots"] = {
                    row["submission_id"]: plan["snapshots"].get(row["submission_id"])
                    for row in selected
                }
            else:
                result["snapshots"] = plan["snapshots"]
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return f"Error: Cannot read operation: {exc}"


def cancel_operation(operation_token: str) -> str:
    """Request cooperative cancellation between rows; never undo accepted writes."""
    try:
        # Separate signal avoids rewriting an executor's in-flight row state.
        pending = [operation_token]
        seen = set()
        while pending:
            token = pending.pop()
            if token in seen:
                continue
            seen.add(token)
            plan = load(token)
            write_artifact(get_artifact_path(f"operation-{token}.cancel"), "cancel")
            child = plan.get("creation_operation")
            if child:
                pending.append(child)
            pending.extend(
                r["operation_token"]
                for r in plan.get("rubric_results", [])
                if isinstance(r, dict) and r.get("operation_token")
            )
        return "Cancellation requested. An in-flight request may finish; subsequent rows will stop. Query the operation for results."
    except Exception as exc:
        return f"Error: Cannot cancel operation: {exc}"


def cancelled(token):
    return get_artifact_path(f"operation-{token}.cancel").exists()
