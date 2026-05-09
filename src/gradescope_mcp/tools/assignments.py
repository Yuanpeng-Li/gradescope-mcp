"""Assignment-related MCP tools."""

import datetime

from gradescopeapi.classes.assignments import (
    update_assignment_date,
    update_assignment_title,
)

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.safety import write_confirmation_required


def _format_datetime(dt: datetime.datetime | None) -> str:
    """Format a datetime for display, handling None."""
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M")


def get_assignments(course_id: str) -> str:
    """Get all assignments for a specific course.

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
    return "\n".join(lines)


def get_assignment_details(course_id: str, assignment_id: str) -> str:
    """Get detailed information about a specific assignment.

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
        return f"Assignment `{assignment_id}` not found in course `{course_id}`."

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

    return "\n".join(lines)


def modify_assignment_dates(
    course_id: str,
    assignment_id: str,
    release_date: str | None = None,
    due_date: str | None = None,
    late_due_date: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Modify the dates of an assignment.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        release_date: New release date in ISO format (YYYY-MM-DDTHH:MM), or None to keep unchanged.
        due_date: New due date in ISO format (YYYY-MM-DDTHH:MM), or None to keep unchanged.
        late_due_date: New late due date in ISO format (YYYY-MM-DDTHH:MM), or None to keep unchanged.
        confirm_write: Must be True to perform the update.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    if not any([release_date, due_date, late_due_date]):
        return "Error: at least one date must be provided."

    def parse_date(date_str: str | None) -> datetime.datetime | None:
        if date_str is None:
            return None
        try:
            return datetime.datetime.fromisoformat(date_str)
        except ValueError:
            raise ValueError(f"Invalid date format: '{date_str}'. Use ISO format: YYYY-MM-DDTHH:MM")

    try:
        rd = parse_date(release_date)
        dd = parse_date(due_date)
        ldd = parse_date(late_due_date)
    except ValueError as e:
        return f"Error: {e}"

    if not confirm_write:
        details = [
            f"course_id=`{course_id}`",
            f"assignment_id=`{assignment_id}`",
        ]
        if release_date:
            details.append(f"release_date={release_date}")
        if due_date:
            details.append(f"due_date={due_date}")
        if late_due_date:
            details.append(f"late_due_date={late_due_date}")
        return write_confirmation_required("modify_assignment_dates", details)

    try:
        conn = get_connection()
        success = update_assignment_date(
            session=conn.session,
            course_id=course_id,
            assignment_id=assignment_id,
            release_date=rd,
            due_date=dd,
            late_due_date=ldd,
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error updating assignment dates: {e}"

    if success:
        updates = []
        if release_date:
            updates.append(f"Release date → {release_date}")
        if due_date:
            updates.append(f"Due date → {due_date}")
        if late_due_date:
            updates.append(f"Late due date → {late_due_date}")
        return f"✅ Assignment `{assignment_id}` dates updated successfully:\n" + "\n".join(f"- {u}" for u in updates)
    else:
        return f"❌ Failed to update dates for assignment `{assignment_id}`. Check your permissions."


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
    except Exception as e:
        error_msg = str(e)
        if "invalid" in error_msg.lower():
            return f"Error: The title '{new_title}' is invalid."
        return f"Error renaming assignment: {e}"

    if success:
        return f"✅ Assignment `{assignment_id}` renamed to '{new_title}'."
    else:
        return f"❌ Failed to rename assignment `{assignment_id}`. Check your permissions."
