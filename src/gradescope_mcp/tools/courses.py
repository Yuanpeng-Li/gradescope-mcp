"""Course-related MCP tools."""

import json

from bs4 import BeautifulSoup

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.common import escape_md_cell


def list_courses() -> str:
    """List all courses for the authenticated user.

    Returns courses grouped by role (instructor vs student).
    """
    try:
        conn = get_connection()
        courses = conn.account.get_courses()
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching courses: {e}"

    lines = []

    instructor_courses = courses.get("instructor", {})
    if instructor_courses:
        lines.append("## Instructor Courses\n")
        for course_id, course in instructor_courses.items():
            lines.append(
                f"- **{course.name}** ({course.full_name})\n"
                f"  - ID: `{course_id}`\n"
                f"  - Semester: {course.semester} {course.year}\n"
                f"  - Assignments: {course.num_assignments}"
            )

    student_courses = courses.get("student", {})
    if student_courses:
        lines.append("\n## Student Courses\n")
        for course_id, course in student_courses.items():
            lines.append(
                f"- **{course.name}** ({course.full_name})\n"
                f"  - ID: `{course_id}`\n"
                f"  - Semester: {course.semester} {course.year}\n"
                f"  - Assignments: {course.num_assignments}"
            )

    if not lines:
        return "No courses found for this account."

    return "\n".join(lines)


def _parse_roster(
    soup: BeautifulSoup, course_id: str, stats: dict | None = None
) -> list[dict]:
    """Parse course roster from the memberships page HTML.

    This is a custom parser that replaces the buggy gradescopeapi
    get_course_members function, which miscounts table columns when
    sections are present.

    If ``stats`` is given it receives ``table_found`` and ``skipped_rows``
    (roster rows without the member-edit button this parser reads them from).
    """
    table = soup.find("table", class_="js-rosterTable")
    if stats is not None:
        stats["table_found"] = table is not None
        stats["skipped_rows"] = 0
    if table is None:
        return []

    # Detect column positions from headers
    headers = [th.text.strip().lower() for th in table.find_all("th")]

    # Find submission column index by header text
    submissions_col = None
    for i, h in enumerate(headers):
        if h.startswith("submission"):
            submissions_col = i
            break

    id_to_role = {"0": "Student", "1": "Instructor", "2": "TA", "3": "Reader"}
    members = []

    for row in soup.find_all("tr", class_="rosterRow"):
        cells = row.find_all("td")
        if not cells:
            continue

        cell0 = cells[0]
        edit_btn = cell0.find("button", class_="rosterCell--editIcon")
        if edit_btn is None:
            if stats is not None:
                stats["skipped_rows"] += 1
            continue

        # Parse member data from the edit button
        data_cm = edit_btn.get("data-cm", "{}")
        try:
            cm = json.loads(data_cm)
        except (json.JSONDecodeError, TypeError):
            cm = {}

        full_name = cm.get("full_name", "")
        first_name = cm.get("first_name", "")
        last_name = cm.get("last_name", "")
        sid = cm.get("sid", "")

        email = edit_btn.get("data-email", "")
        role_id = edit_btn.get("data-role", "")
        role = id_to_role.get(role_id, f"Unknown({role_id})")

        # Parse sections (can be JSON array)
        sections_raw = edit_btn.get("data-sections", "")
        try:
            sections_data = json.loads(sections_raw) if sections_raw else []
            if isinstance(sections_data, list):
                sections = ", ".join(s.get("name", "") for s in sections_data if isinstance(s, dict))
            else:
                sections = str(sections_data)
        except (json.JSONDecodeError, TypeError):
            sections = sections_raw

        # Parse user_id from roster name button
        roster_btn = cell0.find("button", class_="js-rosterName")
        user_id = None
        if roster_btn:
            data_url = roster_btn.get("data-url", "")
            if "user_id=" in data_url:
                user_id = data_url.split("user_id=")[-1]

        # Parse num_submissions from the correct column
        num_submissions = 0
        if submissions_col is not None and submissions_col < len(cells):
            try:
                num_submissions = int(cells[submissions_col].text.strip())
            except (ValueError, TypeError):
                num_submissions = 0

        members.append({
            "full_name": full_name,
            "first_name": first_name,
            "last_name": last_name,
            "sid": sid,
            "email": email,
            "role": role,
            "user_id": user_id,
            "num_submissions": num_submissions,
            "sections": sections,
            "course_id": course_id,
        })

    return members


def get_course_roster(course_id: str) -> str:
    """Get the roster of students and staff for a course.

    Uses a custom HTML parser (the gradescopeapi library's parser
    has a bug with the column indexing when sections are present).

    Args:
        course_id: The Gradescope course ID.
    """
    if not course_id:
        return "Error: course_id is required."

    try:
        conn = get_connection()
        # Fetch the memberships page directly
        url = f"{conn.gradescope_base_url}/courses/{course_id}/memberships"
        resp = conn.session.get(url)
        if resp.status_code != 200:
            return f"Error: Unable to access roster (status {resp.status_code}). Check your permissions."

        soup = BeautifulSoup(resp.text, "html.parser")
        stats: dict = {}
        members = _parse_roster(soup, course_id, stats)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching roster: {e}"

    skipped = stats.get("skipped_rows", 0)
    if not stats.get("table_found"):
        return (
            f"Error: the memberships page for course `{course_id}` has no roster "
            "table (unexpected page). Check the course ID and that you have "
            "instructor or TA access."
        )
    if not members:
        if skipped:
            return (
                f"Error: the roster for course `{course_id}` has {skipped} "
                "row(s), but none carries the member data this tool reads (the "
                "member edit button), so no members can be listed."
            )
        return f"No members found for course `{course_id}`, or you don't have permission to view the roster."

    # Group by role
    by_role: dict[str, list] = {}
    for member in members:
        role = member["role"]
        by_role.setdefault(role, []).append(member)

    lines = [f"## Course Roster (Course {course_id})\n"]
    lines.append(f"**Total members:** {len(members)}\n")
    if skipped:
        lines.append(
            f"⚠️ {skipped} roster row(s) had no member data (no edit button) and "
            "are neither listed nor counted above.\n"
        )

    def cell(value) -> str:
        return escape_md_cell(value) if value else "N/A"

    for role, role_members in sorted(by_role.items()):
        lines.append(f"### {role} ({len(role_members)})\n")
        lines.append("| Name | Email | SID | User ID | Submissions | Sections |")
        lines.append("|------|-------|-----|---------|-------------|----------|")
        for m in sorted(role_members, key=lambda x: x["full_name"] or ""):
            lines.append(
                f"| {cell(m['full_name'])} | {cell(m['email'])} | "
                f"{cell(m['sid'])} | {cell(m['user_id'])} | "
                f"{m['num_submissions']} | {cell(m['sections'])} |"
            )
        lines.append("")

    return "\n".join(lines)
