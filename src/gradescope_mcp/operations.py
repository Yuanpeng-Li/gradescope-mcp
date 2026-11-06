"""Private, durable operation plans. Never infer retry safety from HTTP text alone."""

import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone

from gradescope_mcp.cache import get_artifact_path, write_artifact
from gradescope_mcp.tools.safety import serialized_write


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


def get_operation(operation_token: str) -> str:
    try:
        return json.dumps(load(operation_token), ensure_ascii=False)
    except Exception as exc:
        return f"Error: Cannot read operation: {exc}"


def cancel_operation(operation_token: str) -> str:
    """Request cooperative cancellation between rows; never undo accepted writes."""
    try:
        # Separate signal avoids rewriting an executor's in-flight row state.
        load(operation_token)
        write_artifact(
            get_artifact_path(f"operation-{operation_token}.cancel"), "cancel"
        )
        return "Cancellation requested. An in-flight request may finish; subsequent rows will stop. Query the operation for results."
    except Exception as exc:
        return f"Error: Cannot cancel operation: {exc}"


def cancelled(token):
    return get_artifact_path(f"operation-{token}.cancel").exists()
