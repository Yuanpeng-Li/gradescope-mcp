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


# One lock domain for grading, rubric and grouping changes to a question.
# Reentrant because a planned batch can invoke an existing single-grade tool.
_question_locks = {}
_question_guard = threading.Lock()


@contextlib.contextmanager
def question_write_lock(course_id, question_id):
    from gradescope_mcp.cache import get_artifact_path

    try:
        import fcntl
    except ImportError:
        fcntl = None
        import msvcrt
    import os
    import errno
    import time

    key = (str(course_id), str(question_id))
    with _question_guard:
        lock = _question_locks.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=_WRITE_LOCK_TIMEOUT):
        raise WriteInProgressError("Question is being modified; retry later")
    # Thread-local nesting avoids deadlocking on a second file descriptor.
    held = getattr(_question_local, "held", set())
    fd = None
    try:
        if key not in held:
            import hashlib

            name = hashlib.sha256(repr(key).encode()).hexdigest()
            path = get_artifact_path(f"question-lock-{name}")
            fd = os.open(
                path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            deadline = time.monotonic() + _WRITE_LOCK_TIMEOUT
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        if os.fstat(fd).st_size == 0:
                            os.write(fd, b"0")
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise WriteInProgressError(
                            "Another MCP process is modifying this question"
                        )
                    time.sleep(0.05)
            _question_local.held = held | {key}
        yield
    finally:
        if fd is not None:
            _question_local.held = held
            os.close(fd)
        lock.release()


_question_local = threading.local()


def serialize_question_write(fn):
    """Serialize confirmed mutations, preserving the original MCP signature."""
    import functools
    import inspect

    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        values = signature.bind(*args, **kwargs).arguments
        if values.get("confirm_write") is not True:
            return fn(*args, **kwargs)
        with question_write_lock(values["course_id"], values["question_id"]):
            return fn(*args, **kwargs)

    return wrapped
