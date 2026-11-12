"""Course-related MCP tools."""

import json
import re

from bs4 import BeautifulSoup

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.common import escape_md_cell


# gradescopeapi reads the course box's assignment count with ``.text``, which
# glues the count to a child element: "1 assignmentNo Published Grades".
_GLUED_COUNT_RE = re.compile(r"^(\d+\s+assignments?)(?![a-z])\s*(\S.*)$")


def _assignment_count_text(value) -> str:
    """The course box's assignment count, e.g. "1 assignment · No Published Grades"."""
    text = " ".join(str(value or "").split())
    match = _GLUED_COUNT_RE.match(text)
    if match:
        return f"{match.group(1)} · {match.group(2)}"
    return text or "N/A"


# A course box links to the course home page: "/courses/123" (optionally
# absolute or with a trailing slash). Deeper links are not course boxes.
_COURSE_HREF_RE = re.compile(r"^(?:https?://[^/]+)?/courses/(\d+)/?(?:[?#].*)?$")

_ROLE_TITLES = {
    "instructor": "Instructor Courses",
    "student": "Student Courses",
    "other": "Other Courses",
}


def _classes(tag) -> list[str]:
    value = tag.get("class") or []
    return value.split() if isinstance(value, str) else list(value)


def _text(tag) -> str:
    return " ".join(tag.get_text(" ").split()) if tag is not None else ""


def _role_for_heading(heading: str | None, has_create_button: bool) -> str:
    """Map an account-page section heading to a role group.

    Gradescope titles its sections "Instructor Courses", "Course Staff",
    "Student Courses" and similar. Without any heading, fall back to the
    "Create Course" button (only instructors see it); a page with neither
    states no role, which is reported as "other" rather than guessed: a TA
    has no Create Course button but is not a student.
    """
    if heading is None:
        return "instructor" if has_create_button else "other"
    text = heading.lower()
    if "student" in text or "enrolled" in text:
        return "student"
    if any(
        word in text
        for word in ("instructor", "staff", "teaching", "reader", "grader")
    ):
        return "instructor"
    return "other"


def _is_course_box(tag) -> bool:
    if tag.name != "a" or not _COURSE_HREF_RE.match(str(tag.get("href") or "").strip()):
        return False
    return (
        "courseBox" in _classes(tag)
        or tag.find(class_="courseBox--shortname") is not None
    )


def _parse_account_courses(soup: BeautifulSoup) -> dict:
    """Parse the course boxes of a Gradescope account page.

    Replaces gradescopeapi's ``get_courses_info``, which only found course
    links that are ``<a>`` siblings after a term heading and decided
    instructor vs student solely by the "Create Course" button, so a TA
    without that button or a page whose boxes sit in other wrapper elements
    could come back as a silent empty list.

    Walks the page in document order, so each course box is attributed to
    the nearest preceding section heading (``.pageHeading``) and term
    (``.courseList--term``) whatever containers wrap it. Returns
    ``{"courses": [...], "boxes": n, "unreadable": n, "course_links": n,
    "has_container": bool}``.
    """
    has_create_button = soup.select_one(".js-createNewCourse") is not None
    heading = None
    term = None
    courses: list[dict] = []
    boxes = unreadable = 0
    seen: set[str] = set()
    link_ids: set[str] = set()
    course_links = 0
    for a in soup.find_all("a", href=True):
        match = _COURSE_HREF_RE.match(str(a["href"]).strip())
        if match:
            course_links += 1
            link_ids.add(match.group(1))
    box_ids: set[str] = set()
    for tag in soup.find_all(True):
        classes = _classes(tag)
        if tag.name in ("h1", "h2", "h3") and "pageHeading" in classes:
            heading = _text(tag) or None
            term = None
        elif "courseList--term" in classes:
            term = _text(tag) or None
        elif _is_course_box(tag):
            boxes += 1
            course_id = _COURSE_HREF_RE.match(str(tag["href"]).strip()).group(1)
            box_ids.add(course_id)
            short = _text(tag.find(class_="courseBox--shortname"))
            if not short:
                unreadable += 1
                continue
            if course_id in seen:
                continue
            seen.add(course_id)
            assignments = tag.find(class_="courseBox--assignments")
            courses.append(
                {
                    "id": course_id,
                    "name": short,
                    "full_name": _text(tag.find(class_="courseBox--name")),
                    "term": term or "",
                    "heading": heading or "",
                    "role": _role_for_heading(heading, has_create_button),
                    # The separator keeps "1 assignment" apart from a child
                    # label such as "No Published Grades".
                    "num_assignments": (
                        _assignment_count_text(assignments.get_text("\n"))
                        if assignments is not None
                        else "N/A"
                    ),
                }
            )
    has_container = (
        soup.select_one("#account-show") is not None
        or soup.select_one(".courseList") is not None
    )
    return {
        "courses": courses,
        "boxes": boxes,
        "unreadable": unreadable,
        "course_links": course_links,
        "has_container": has_container,
        # Course IDs linked on the page that no course box carries: markup
        # this parser does not recognise, so those courses are not listed.
        "unmatched_ids": sorted(link_ids - box_ids, key=int),
    }


def _fetch_account_courses(conn) -> dict | str:
    """Fetch and parse the account page, falling back from /account to /.

    Returns the parse result, or an error string for an HTTP failure.
    """
    base = conn.gradescope_base_url
    parsed = None
    for path in ("/account", "/"):
        resp = conn.session.get(f"{base}{path}")
        if resp.status_code != 200:
            if parsed is not None:
                break  # keep what /account gave
            return (
                f"Error: Unable to load the Gradescope account page {path} "
                f"(HTTP {resp.status_code}); cannot list courses."
            )
        parsed = _parse_account_courses(BeautifulSoup(resp.text, "html.parser"))
        if parsed["has_container"] or parsed["boxes"] or parsed["course_links"]:
            break
    return parsed


def list_courses() -> str:
    """List all courses for the authenticated user.

    Returns courses grouped by role (instructor/staff vs student). Tells a
    genuinely empty account apart from a page this parser could not read.
    """
    try:
        conn = get_connection()
        parsed = _fetch_account_courses(conn)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching courses: {e}"
    if isinstance(parsed, str):
        return parsed

    courses = parsed["courses"]
    direct = (
        "A known course ID can still be used directly with the other tools "
        "(e.g. tool_get_assignments)."
    )
    if not courses:
        if parsed["boxes"] or parsed["course_links"]:
            return (
                "Error: Could not parse the course list: the account page has "
                f"{parsed['course_links']} course link(s) ({parsed['boxes']} "
                f"course box(es), {parsed['unreadable']} without a readable "
                "name), but none could be read. Gradescope's page layout may "
                f"have changed. {direct}"
            )
        if not parsed["has_container"]:
            return (
                "Error: Could not parse the course list: the account page has "
                f"no course list section (unexpected page). {direct}"
            )
        return f"No courses found for this account. {direct}"

    lines = []
    for role in ("instructor", "student", "other"):
        group = [c for c in courses if c["role"] == role]
        if not group:
            continue
        lines.append(("\n" if lines else "") + f"## {_ROLE_TITLES[role]}\n")
        for course in group:
            full = f" ({course['full_name']})" if course["full_name"] else ""
            entry = (
                f"- **{course['name']}**{full}\n"
                f"  - ID: `{course['id']}`\n"
                f"  - Semester: {course['term'] or 'N/A'}\n"
                f"  - Assignments: {course['num_assignments']}"
            )
            if role == "other":
                section = course["heading"] or "not stated on the page"
                entry += f"\n  - Section: {section}"
            lines.append(entry)
    if parsed["unreadable"]:
        lines.append(
            f"\n⚠️ {parsed['unreadable']} course box(es) on the account page "
            "had no readable course name and are not listed."
        )
    if parsed.get("unmatched_ids"):
        ids = ", ".join(f"`{i}`" for i in parsed["unmatched_ids"])
        lines.append(
            f"\n⚠️ The account page also links course(s) {ids} in a layout this "
            "parser does not recognise, so they are not listed above (role and "
            f"name unknown). {direct}"
        )
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
