"""Assignment-related MCP tools."""

import contextlib
import datetime
import re
import threading

import requests
from bs4 import BeautifulSoup
from gradescopeapi.classes.assignments import (
    AssignmentUpdateError,
    InvalidTitleName,
    update_assignment_title,
    update_autograder_image_name,
)

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.safety import write_confirmation_required


def _format_utc_offset(offset: datetime.timedelta) -> str:
    """``UTC`` for a zero offset, else e.g. ``UTC-07:00``."""
    minutes = int(offset.total_seconds() // 60)
    if minutes == 0:
        return "UTC"
    sign = "-" if minutes < 0 else "+"
    hours, minutes = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _format_datetime(dt: datetime.datetime | None) -> str:
    """Format a datetime for display, handling None.

    A timezone-aware value keeps its UTC offset (``2026-10-01 23:59
    UTC-07:00``): Gradescope may report the same instant in UTC or in the
    course's offset, and the wall-clock text alone would be ambiguous.
    """
    if dt is None:
        return "N/A"
    text = dt.strftime("%Y-%m-%d %H:%M")
    offset = dt.utcoffset()
    return text if offset is None else f"{text} {_format_utc_offset(offset)}"


# Added below listings that show a date with a UTC offset.
_DATE_OFFSET_NOTE = (
    "_Dates are shown with the UTC offset Gradescope reported. "
    "modify_assignment_dates takes course-local wall-clock times: convert "
    "first, or read the current values from its preview, which shows them "
    "in course-local time._"
)


def _has_offset(*values: datetime.datetime | None) -> bool:
    return any(v is not None and v.utcoffset() is not None for v in values)


# ---------------------------------------------------------------------------
# Serialized writes (shared with extensions.py)
# ---------------------------------------------------------------------------

# How long a confirmed write waits for another write to the same object.
_WRITE_LOCK_TIMEOUT = 300.0
_write_locks: dict[tuple[str, ...], threading.Lock] = {}
_write_locks_guard = threading.Lock()


class WriteInProgressError(Exception):
    """Another call is still writing the same Gradescope object."""


@contextlib.contextmanager
def serialized_write(*key: str):
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
    if not lock.acquire(timeout=_WRITE_LOCK_TIMEOUT):
        raise WriteInProgressError(
            "another change to the same Gradescope settings is still running; "
            "try again when it has finished."
        )
    try:
        yield
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Date arguments (shared with extensions.py)
# ---------------------------------------------------------------------------

# A time of day is mandatory: Python would read a bare "2026-10-02" as 00:00,
# the very start of that day, which is almost never what "due Oct 2" means.
_DATE_INPUT_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.(?P<frac>\d+))?)?"
    r"(?P<offset>Z|[+-]\d{2}(?::?\d{2})?)?$"
)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Format Gradescope's assignment form uses for its date fields.
_FORM_DATE_FORMAT = "%Y-%m-%dT%H:%M"


def _parse_utc_offset(text: str) -> datetime.timezone:
    if text == "Z":
        return datetime.timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4] or 0)
    if hours > 23 or minutes > 59:
        raise ValueError(text)
    return datetime.timezone(sign * datetime.timedelta(hours=hours, minutes=minutes))


def parse_date_input(
    value: str | None, field: str, *, allow_offset: bool
) -> datetime.datetime | None:
    """Parse an optional date argument supplied by an MCP client.

    ``None`` and blank strings mean "not provided". Otherwise the value must
    be ``YYYY-MM-DDTHH:MM`` (``:00`` seconds allowed); a UTC offset (``Z``,
    ``+02:00``) is accepted only when ``allow_offset`` is true and yields an
    aware datetime. Date-only values are rejected rather than read as 00:00.

    Raises:
        ValueError: with a message starting "Invalid date".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    hint = "Use ISO format: YYYY-MM-DDTHH:MM"
    if _DATE_ONLY_RE.match(text):
        raise ValueError(
            f"Invalid date format for {field}: '{value}' has no time of day. "
            f"{hint}, e.g. '{text}T23:59' for the end of that day (a bare "
            "date would mean 00:00, the very start of it)."
        )
    match = _DATE_INPUT_RE.match(text)
    if not match:
        raise ValueError(f"Invalid date format for {field}: '{value}'. {hint}")
    if int(match["second"] or 0) or int(match["frac"] or 0):
        raise ValueError(
            f"Invalid date for {field}: '{value}'. Gradescope stores whole "
            "minutes; drop the seconds."
        )
    try:
        parsed = datetime.datetime.strptime(
            f"{match['date']}T{match['hour']}:{match['minute']}", _FORM_DATE_FORMAT
        )
    except ValueError:
        raise ValueError(
            f"Invalid date for {field}: '{value}' is not a real calendar date and time."
        ) from None

    offset = match["offset"]
    if offset:
        if not allow_offset:
            raise ValueError(
                f"Invalid date for {field}: '{value}' includes a UTC offset. "
                "Assignment dates are wall-clock times in the course's "
                "timezone; give YYYY-MM-DDTHH:MM without an offset (convert "
                "to the course's local time first)."
            )
        try:
            parsed = parsed.replace(tzinfo=_parse_utc_offset(offset))
        except ValueError:
            raise ValueError(
                f"Invalid date for {field}: '{value}' has an invalid UTC offset."
            ) from None
    return parsed


def check_date_order(
    dates: list[tuple[str, datetime.datetime | None]],
) -> str | None:
    """Return an error message unless the provided dates are in order.

    ``dates`` lists (name, value) pairs in their required order; ``None``
    values are skipped. All values must be either naive or aware.
    """
    present = [(name, value) for name, value in dates if value is not None]
    for (first, a), (second, b) in zip(present, present[1:]):
        if a > b:
            return (
                f"{first} ({a.isoformat(timespec='minutes')}) is after "
                f"{second} ({b.isoformat(timespec='minutes')}). Dates must be "
                "in order: release_date <= due_date <= late_due_date."
            )
    return None


def get_assignments(course_id: str) -> str:
    """Get all assignments for a specific course.

    Dates Gradescope reports with a UTC offset are shown with it.

    Args:
        course_id: The Gradescope course ID.
    """
    if not course_id:
        return "Error: course_id is required."

    try:
        conn = get_connection()
        assignments = conn.account.get_assignments(course_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching assignments: {e}"

    if not assignments:
        return f"No assignments found for course `{course_id}`."

    lines = [f"## Assignments for Course {course_id}\n"]
    lines.append("| # | Name | ID | Release Date | Due Date | Late Due | Status | Grade |")
    lines.append("|---|------|-----|-------------|----------|----------|--------|-------|")

    for i, a in enumerate(assignments, 1):
        # Use ``is None`` rather than ``or`` so a real score of 0 / 0.0 is not
        # collapsed to "N/A" — students who scored zero deserve to see zero.
        grade = "N/A" if a.grade is None else a.grade
        max_grade = "N/A" if a.max_grade is None else a.max_grade
        lines.append(
            f"| {i} | {a.name} | `{a.assignment_id}` | "
            f"{_format_datetime(a.release_date)} | "
            f"{_format_datetime(a.due_date)} | "
            f"{_format_datetime(a.late_due_date)} | "
            f"{a.submissions_status or 'N/A'} | "
            f"{grade}/{max_grade} |"
        )

    lines.append(f"\n**Total assignments:** {len(assignments)}")
    if any(_has_offset(a.release_date, a.due_date, a.late_due_date) for a in assignments):
        lines.append(_DATE_OFFSET_NOTE)
    return "\n".join(lines)


def get_assignment_details(course_id: str, assignment_id: str) -> str:
    """Get detailed information about a specific assignment.

    Dates Gradescope reports with a UTC offset are shown with it.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        conn = get_connection()
        assignments = conn.account.get_assignments(course_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching assignment details: {e}"

    # Find the specific assignment
    target = None
    for a in assignments:
        if str(a.assignment_id) == str(assignment_id):
            target = a
            break

    if target is None:
        return f"Error: assignment `{assignment_id}` not found in course `{course_id}`."

    grade = "N/A" if target.grade is None else target.grade
    max_grade = "N/A" if target.max_grade is None else target.max_grade
    lines = [
        f"## Assignment Details\n",
        f"- **Name:** {target.name}",
        f"- **Assignment ID:** `{target.assignment_id}`",
        f"- **Release Date:** {_format_datetime(target.release_date)}",
        f"- **Due Date:** {_format_datetime(target.due_date)}",
        f"- **Late Due Date:** {_format_datetime(target.late_due_date)}",
        f"- **Submission Status:** {target.submissions_status or 'N/A'}",
        f"- **Grade:** {grade} / {max_grade}",
    ]
    if _has_offset(target.release_date, target.due_date, target.late_due_date):
        lines.append(f"\n{_DATE_OFFSET_NOTE}")

    return "\n".join(lines)


# (key, tool argument, form field) for the dates on the assignment settings
# form at /courses/{cid}/assignments/{aid}/edit.
_DATE_FORM_FIELDS = (
    ("release", "release_date", "assignment[release_date_string]"),
    ("due", "due_date", "assignment[due_date_string]"),
    ("late", "late_due_date", "assignment[hard_due_date_string]"),
)
_ALLOW_LATE_FIELD = "assignment[allow_late_submissions]"
# Markup Gradescope (Rails) uses when it re-renders a form with errors.
_FORM_ERROR_SELECTORS = (
    ".form--requiredFieldStar.error",
    ".field_with_errors",
    "#error_explanation",
)
_COURSE_LOCAL_NOTE = (
    "Times are course-local wall-clock times: Gradescope interprets them in "
    "the course's timezone."
)


class _DateFormError(Exception):
    """The assignment's current date settings could not be read safely."""


def _assignment_urls(conn, course_id: str, assignment_id: str) -> tuple[str, str]:
    """Return (settings form URL, form POST URL) for an assignment."""
    base = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}"
    return f"{base}/edit", base


def _read_date_form(conn, course_id: str, assignment_id: str) -> dict:
    """Read the current dates and late-submission flag from the settings form.

    Returns ``token`` (the form's authenticity token); ``release``, ``due``
    and ``late`` as ``YYYY-MM-DDTHH:MM`` strings (``""`` when blank, ``None``
    when unreadable); ``allow_late`` (``None`` when the checkbox is missing);
    and ``problems``, which says why a field is ``None``.
    """
    edit_url, _ = _assignment_urls(conn, course_id, assignment_id)
    resp = conn.session.get(edit_url)
    if resp.status_code != 200:
        raise _DateFormError(
            f"the assignment settings page returned HTTP {resp.status_code}"
        )
    soup = BeautifulSoup(resp.text, "html.parser")
    token = soup.select_one('input[name="authenticity_token"]')
    if token is None or not token.get("value"):
        raise _DateFormError(
            "the assignment settings page has no edit form (unexpected page; "
            "check the IDs and that you have instructor access)"
        )

    state: dict = {"token": token["value"], "problems": {}}
    for key, label, name in _DATE_FORM_FIELDS:
        field = soup.find("input", attrs={"name": name})
        if field is None:
            state[key] = None
            state["problems"][key] = f"the settings page has no {label} field"
            continue
        raw = (field.get("value") or "").strip()
        if not raw:
            state[key] = ""
            continue
        try:
            parsed = parse_date_input(raw, label, allow_offset=False)
        except ValueError:
            state[key] = None
            state["problems"][key] = (
                f"the current {label} on the settings page has an "
                f"unrecognized format ({raw!r})"
            )
            continue
        state[key] = parsed.strftime(_FORM_DATE_FORMAT)

    state["allow_late"] = None
    for box in soup.find_all("input", attrs={"name": _ALLOW_LATE_FIELD}):
        if (box.get("type") or "").lower() == "checkbox":
            state["allow_late"] = box.has_attr("checked")
            break
    return state


def _plan_date_update(current: dict, requested: dict) -> dict:
    """Merge the requested dates into the current settings.

    Gradescope's form update sets all date fields and the late-submission
    flag together, so every omitted value is carried over from ``current``.
    Late submissions are switched on only when a late due date is requested.
    """

    def keep(key: str, label: str) -> str:
        if current[key] is None:
            reason = current["problems"].get(key, f"the current {label} is unreadable")
            raise _DateFormError(
                f"{reason}, so it can't be kept unchanged; pass {label} explicitly"
            )
        return current[key]

    plan: dict = {}
    for key, label, _ in _DATE_FORM_FIELDS[:2]:
        value = requested[key]
        plan[key] = value.strftime(_FORM_DATE_FORMAT) if value is not None else keep(key, label)

    if requested["late"] is not None:
        plan["allow_late"] = True
        plan["late"] = requested["late"].strftime(_FORM_DATE_FORMAT)
    elif current["allow_late"] is None:
        raise _DateFormError(
            "the settings page has no allow_late_submissions checkbox, so it "
            "is unknown whether late submissions are on; pass late_due_date "
            "explicitly"
        )
    else:
        plan["allow_late"] = current["allow_late"]
        if plan["allow_late"]:
            plan["late"] = keep("late", "late_due_date")
        else:
            # Ignored by Gradescope while late submissions are off; sent back
            # as-is so the hidden value isn't changed either.
            plan["late"] = current["late"] or ""
    return plan


def _plan_order_error(plan: dict) -> str | None:
    def parsed(key: str) -> datetime.datetime | None:
        return datetime.datetime.strptime(plan[key], _FORM_DATE_FORMAT) if plan[key] else None

    dates = [("release_date", parsed("release")), ("due_date", parsed("due"))]
    if plan["allow_late"]:
        dates.append(("late_due_date", parsed("late")))
    return check_date_order(dates)


def _show_setting(value) -> str:
    if value is None:
        return "(unreadable)"
    if isinstance(value, bool):
        return "on" if value else "off"
    return value or "(none)"


def _requested_keys(requested: dict) -> frozenset[str]:
    """Plan keys the caller asked to set (a late due date also sets the flag)."""
    keys = {key for key, value in requested.items() if value is not None}
    if "late" in keys:
        keys.add("allow_late")
    return frozenset(keys)


def _already_set(plan: dict, before: dict, requested: frozenset[str]) -> bool:
    """True when every requested value already had that value in ``before``."""
    return all(before[key] == plan[key] for key in requested)


def _describe_plan(
    plan: dict,
    before: dict | None,
    verb: str,
    requested: frozenset[str] = frozenset(),
    already: str = "already the current value",
) -> list[str]:
    """One line per form setting: the value sent and how it compares to ``before``.

    A requested value equal to ``before`` is labelled ``already`` rather than
    "unchanged", so a write retried after it already went through doesn't
    read as if nothing was changed.
    """

    def line(name: str, key: str) -> str:
        text = f"{name}={_show_setting(plan[key])}"
        if before is None:
            return text
        if before[key] is None:
            return f"{text} (current value unreadable)"
        if before[key] == plan[key]:
            return f"{text} ({already if key in requested else 'unchanged'})"
        return f"{text} ({verb} {_show_setting(before[key])})"

    late = line("late_due_date", "late")
    if not plan["allow_late"]:
        late += "; no effect while late submissions are off"
    return [
        line("release_date", "release"),
        line("due_date", "due"),
        line("allow_late_submissions", "allow_late"),
        late,
    ]


def _submit_date_form(
    conn, course_id: str, assignment_id: str, token: str, plan: dict
) -> requests.Response:
    """PATCH the settings form with every date field set explicitly.

    Same multipart request as gradescopeapi's ``update_assignment_date``,
    except that omitted dates are not blanked and the late-submission flag
    comes from the plan instead of "was a late due date given".
    """
    edit_url, post_url = _assignment_urls(conn, course_id, assignment_id)
    fields = [
        ("utf8", "✓"),
        ("_method", "patch"),
        ("authenticity_token", token),
        ("assignment[release_date_string]", plan["release"]),
        ("assignment[due_date_string]", plan["due"]),
        (_ALLOW_LATE_FIELD, "1" if plan["allow_late"] else "0"),
        ("assignment[hard_due_date_string]", plan["late"]),
        ("commit", "Save"),
    ]
    return conn.session.post(
        post_url,
        files=[(name, (None, value)) for name, value in fields],
        headers={"Referer": edit_url},
    )


def _form_error_text(html: str | None) -> str | None:
    """Return validation messages from a re-rendered form, if any."""
    soup = BeautifulSoup(html or "", "html.parser")
    messages: list[str] = []
    for selector in _FORM_ERROR_SELECTORS:
        for element in soup.select(selector):
            # The required-field star sits inside the field's label.
            holder = element.parent if "requiredFieldStar" in selector and element.parent else element
            text = " ".join(holder.get_text(" ", strip=True).split())[:200]
            if text and text not in messages:
                messages.append(text)
    return "; ".join(messages) or None


def _verify_dates(plan: dict, after: dict) -> tuple[list[str], list[str]]:
    """Compare the plan with re-read settings: (mismatches, unverifiable names)."""
    checks = [
        ("release_date", "release"),
        ("due_date", "due"),
        ("allow_late_submissions", "allow_late"),
    ]
    if plan["allow_late"]:
        checks.append(("late_due_date", "late"))
    mismatches: list[str] = []
    unknown: list[str] = []
    for name, key in checks:
        if after[key] is None:
            unknown.append(name)
        elif after[key] != plan[key]:
            mismatches.append(
                f"{name}: sent {_show_setting(plan[key])}, Gradescope shows "
                f"{_show_setting(after[key])}"
            )
    return mismatches, unknown


def modify_assignment_dates(
    course_id: str,
    assignment_id: str,
    release_date: str | None = None,
    due_date: str | None = None,
    late_due_date: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Modify the dates of an assignment.

    Dates are course-local wall-clock times (``YYYY-MM-DDTHH:MM``, no UTC
    offset): Gradescope interprets them in the course's timezone. Its form
    update replaces all dates and the allow-late-submissions flag at once,
    so the current settings are read first and every omitted value is sent
    back unchanged. Supplying ``late_due_date`` turns late submissions on.
    The preview lists every value that will be sent next to the current one
    (so it fails when the settings can't be read); after writing, the
    settings are read back and compared. Confirmed changes to the same
    assignment run one at a time, so concurrent calls can't revert each
    other.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        release_date: New release date (YYYY-MM-DDTHH:MM), or None/"" to keep unchanged.
        due_date: New due date (YYYY-MM-DDTHH:MM), or None/"" to keep unchanged.
        late_due_date: New late due date (YYYY-MM-DDTHH:MM), or None/"" to keep unchanged.
        confirm_write: Must be True to perform the update.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        requested = {
            "release": parse_date_input(release_date, "release_date", allow_offset=False),
            "due": parse_date_input(due_date, "due_date", allow_offset=False),
            "late": parse_date_input(late_due_date, "late_due_date", allow_offset=False),
        }
    except ValueError as e:
        return f"Error: {e}"

    if all(value is None for value in requested.values()):
        return "Error: at least one date must be provided."

    order_error = check_date_order(
        [(label, requested[key]) for key, label, _ in _DATE_FORM_FIELDS]
    )
    if order_error:
        return f"Error: {order_error}"

    if not confirm_write:
        return _change_assignment_dates(course_id, assignment_id, requested, False)
    try:
        with serialized_write("assignment dates", course_id, assignment_id):
            return _change_assignment_dates(course_id, assignment_id, requested, True)
    except WriteInProgressError:
        return (
            f"Error: another date change for assignment `{assignment_id}` is "
            "still running; try again when it has finished. Nothing was changed."
        )


def _change_assignment_dates(
    course_id: str, assignment_id: str, requested: dict, confirm_write: bool
) -> str:
    """Read, plan and (with ``confirm_write``) write and verify a date change."""
    header = [f"course_id=`{course_id}`", f"assignment_id=`{assignment_id}`"]
    requested_keys = _requested_keys(requested)

    try:
        conn = get_connection()
        current = _read_date_form(conn, course_id, assignment_id)
    except AuthError as e:
        # Also for the preview: it must show every value that will be sent,
        # and the write needs the same login anyway.
        return f"Authentication error: {e}"
    except Exception as e:
        return (
            f"Error: cannot read the current dates of assignment "
            f"`{assignment_id}`: {str(e) or repr(e)}. Nothing was changed."
        )

    try:
        plan = _plan_date_update(current, requested)
    except _DateFormError as e:
        return (
            f"Error: cannot safely update assignment `{assignment_id}`: {e}. "
            "Nothing was changed."
        )
    order_error = _plan_order_error(plan)
    if order_error:
        return (
            f"Error: {order_error} Dates you don't pass keep their current "
            "values, so pass the conflicting one as well. Nothing was changed."
        )

    if not confirm_write:
        details = header + _describe_plan(plan, current, "currently", requested_keys) + [
            _COURSE_LOCAL_NOTE,
            "All four settings above are sent together; values marked "
            "unchanged are re-sent as they are.",
        ]
        if _already_set(plan, current, requested_keys):
            details.append(
                "Gradescope already has every requested value, so confirming "
                "changes nothing."
            )
        return write_confirmation_required("modify_assignment_dates", details)

    try:
        resp = _submit_date_form(conn, course_id, assignment_id, current["token"], plan)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return (
            f"Error updating assignment dates: the request failed ({e!r}); "
            "Gradescope may or may not have applied it. Check with "
            "get_assignment_details."
        )

    if resp.status_code >= 400:
        return (
            f"❌ Gradescope rejected the date change for assignment "
            f"`{assignment_id}` (HTTP {resp.status_code}). Check your permissions."
        )

    summary = "\n".join(
        f"- {line}"
        for line in _describe_plan(
            plan, current, "was", requested_keys, "already set before this write"
        )
    )
    if _already_set(plan, current, requested_keys):
        # E.g. a call re-run after its first write went through: the dates
        # did change, just not in this run.
        summary += (
            "\nGradescope already had every requested value when this call "
            "read the settings, so this write changed nothing itself (an "
            "earlier attempt of the same change, e.g. one interrupted by a "
            "session expiry, may have applied it)."
        )
    form_errors = _form_error_text(resp.text)
    try:
        after = _read_date_form(conn, course_id, assignment_id)
    except Exception as e:
        if form_errors:
            return (
                f"❌ Gradescope rejected the date change for assignment "
                f"`{assignment_id}`: {form_errors}"
            )
        return (
            f"⚠️ The date change for assignment `{assignment_id}` was submitted "
            f"(HTTP {resp.status_code}) but could not be verified: {e}. "
            f"Check with get_assignment_details. Sent:\n{summary}"
        )

    mismatches, unknown = _verify_dates(plan, after)
    if mismatches:
        lines = [
            f"❌ Gradescope did not apply the date change for assignment "
            f"`{assignment_id}` as sent (re-read from the settings page):"
        ]
        lines += [f"- {m}" for m in mismatches]
        if form_errors:
            lines.append(f"- Gradescope reported: {form_errors}")
        return "\n".join(lines)
    if unknown:
        return (
            f"⚠️ The date change for assignment `{assignment_id}` was submitted, "
            f"but {', '.join(unknown)} could not be read back to confirm it. "
            f"Sent (course-local times):\n{summary}"
        )
    return (
        f"✅ Assignment `{assignment_id}` dates updated successfully (read back "
        f"from Gradescope; course-local times):\n{summary}"
    )


def rename_assignment(
    course_id: str,
    assignment_id: str,
    new_title: str,
    confirm_write: bool = False,
) -> str:
    """Rename an assignment.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        new_title: The new title for the assignment. Cannot be all whitespace.
        confirm_write: Must be True to perform the rename.
    """
    if not all([course_id, assignment_id, new_title]):
        return "Error: course_id, assignment_id, and new_title are all required."

    if not new_title.strip():
        return "Error: new_title cannot be all whitespace."

    if not confirm_write:
        return write_confirmation_required(
            "rename_assignment",
            [
                f"course_id=`{course_id}`",
                f"assignment_id=`{assignment_id}`",
                f"new_title={new_title}",
            ],
        )

    try:
        conn = get_connection()
        success = update_assignment_title(
            session=conn.session,
            course_id=course_id,
            assignment_id=assignment_id,
            assignment_name=new_title,
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except InvalidTitleName:
        return f"Error: The title '{new_title}' is invalid."
    except AssignmentUpdateError as e:
        return f"❌ Gradescope rejected the rename of assignment `{assignment_id}`: {e}"
    except Exception as e:
        return f"Error renaming assignment: {e}"

    if success:
        return f"✅ Assignment `{assignment_id}` renamed to '{new_title}'."
    else:
        return f"❌ Failed to rename assignment `{assignment_id}`. Check your permissions."


def update_autograder_image(
    course_id: str,
    assignment_id: str,
    image_name: str,
    confirm_write: bool = False,
) -> str:
    """Change the Docker Hub image a programming assignment's autograder uses.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID. Must be a programming assignment
            whose autograder is configured with a Docker Hub image.
        image_name: Docker Hub image reference, e.g.
            ``gradescope/autograder-base:ubuntu-22.04``.
        confirm_write: Must be True to perform the update.
    """
    if not all([course_id, assignment_id, image_name]):
        return "Error: course_id, assignment_id, and image_name are all required."

    image_name = image_name.strip()
    if not image_name or any(ch.isspace() for ch in image_name):
        return (
            "Error: image_name must be a Docker image reference without "
            "whitespace, e.g. `gradescope/autograder-base:ubuntu-22.04`."
        )

    if not confirm_write:
        return write_confirmation_required(
            "update_autograder_image",
            [
                f"course_id=`{course_id}`",
                f"assignment_id=`{assignment_id}`",
                f"image_name=`{image_name}`",
                "Future autograder runs for this assignment use the new image.",
                "Gradescope may accept an image that does not exist on Docker "
                "Hub; run a test submission after updating.",
            ],
        )

    try:
        conn = get_connection()
        success = update_autograder_image_name(
            session=conn.session,
            course_id=course_id,
            assignment_id=assignment_id,
            image_name=image_name,
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        return (
            f"Error updating autograder image (status {status}). Check that "
            f"assignment `{assignment_id}` is a programming assignment with a "
            "Docker image autograder and that you have instructor access."
        )
    except Exception as e:
        return f"Error updating autograder image: {e!r}"

    if success:
        return (
            f"✅ Autograder image for assignment `{assignment_id}` set to "
            f"`{image_name}`. Run a test submission to confirm the autograder "
            "still works."
        )
    return (
        f"❌ Gradescope did not accept image `{image_name}` for assignment "
        f"`{assignment_id}` (it reported the Docker image was not found)."
    )
