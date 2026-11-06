"""Safety helpers for write-capable MCP tools."""

import contextlib
import threading


def write_confirmation_required(action: str, details: list[str]) -> str:
    """Return a standardized no-op preview for mutating tools."""
    lines = [
        f"Write confirmation required for `{action}`.",
        "No changes were made.",
    ]
    lines.extend(f"- {detail}" for detail in details)
    lines.append(
        "- Show this preview to the user; only after they explicitly approve, "
        "re-run with `confirm_write=True` to execute this change."
    )
    return "\n".join(lines)


# How long a confirmed write waits for another write to the same object.
_WRITE_LOCK_TIMEOUT = 300.0
_write_locks: dict[tuple[str, ...], threading.Lock] = {}
_write_locks_guard = threading.Lock()


class WriteInProgressError(Exception):
    """Another call is still writing the same Gradescope object."""


@contextlib.contextmanager
def serialized_write(*key: str, timeout: float = _WRITE_LOCK_TIMEOUT):
    """Hold a process-wide lock for one Gradescope object during a write.

    The date and extension writes read the current settings, merge the
    request into them and send everything back. mcp runs sync tools on
    worker threads, so two approved calls on the same object could both
    read the same state, and the later write would silently revert the
    earlier one. Holding the lock across read, write and read-back makes
    such calls run one after the other.

    Raises:
        WriteInProgressError: if another call holds the lock for longer than
            ``_WRITE_LOCK_TIMEOUT`` seconds.
    """
    key = tuple(str(part) for part in key)
    with _write_locks_guard:
        lock = _write_locks.setdefault(key, threading.Lock())
    if not lock.acquire(timeout=timeout):
        raise WriteInProgressError(
            "another change to the same Gradescope settings is still running; "
            "try again when it has finished."
        )
    try:
        yield
    finally:
        lock.release()
