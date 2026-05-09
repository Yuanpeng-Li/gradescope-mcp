"""Regrade request management tools.

These tools allow instructors/TAs to view, review, and manage regrade requests
submitted by students. Data is parsed from the Gradescope regrade request pages
and the SubmissionGrader React component.
"""

import json
import re

from bs4 import BeautifulSoup

from gradescope_mcp.auth import get_connection, AuthError


# Placeholder strings Gradescope uses to mean "no value yet" — these must NOT
# count as completed even though they're non-empty.
_REGRADE_PLACEHOLDERS = frozenset({"", "-", "—", "–", "pending", "n/a", "none", "tbd"})

# Likely header labels for the column that records when a regrade was decided.
_REGRADE_COMPLETED_HEADERS = ("completed", "completed at", "decided", "decided at", "resolved")


def get_regrade_requests(course_id: str, assignment_id: str) -> str:
    """List all regrade requests for an assignment.

    Returns a table of pending and completed regrade requests with student name,
    question, grader, and status. Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: course_id and assignment_id are required."

    try:
        conn = get_connection()
        url = (
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/assignments/{assignment_id}/regrade_requests"
        )
        resp = conn.session.get(url)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching regrade requests: {e}"

    if resp.status_code != 200:
        return f"Error: Cannot access regrade requests (status {resp.status_code})."

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        return f"No regrade requests found for assignment `{assignment_id}`. (Regrade requests may not be enabled.)"

    # Header-aware column resolution. Gradescope drops the Sections column for
    # courses without sections, which used to shift every cell index left.
    headers = []
    thead = table.find("thead")
    if thead is not None:
        header_row = thead.find("tr")
        if header_row is not None:
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]
    if not headers:
        first_row = table.find("tr")
        if first_row is not None:
            ths = first_row.find_all("th")
            if ths:
                headers = [th.get_text(strip=True) for th in ths]

    def _col(*names: str) -> int | None:
        wanted = {n.lower() for n in names}
        for idx, h in enumerate(headers):
            if (h or "").strip().lower() in wanted:
                return idx
        return None

    student_idx = _col("student", "name", "user")
    question_idx = _col("question")
    grader_idx = _col("grader", "graded by", "last graded by")
    completed_idx = _col(*_REGRADE_COMPLETED_HEADERS)

    body = table.find("tbody") or table
    data_rows = [tr for tr in body.find_all("tr") if tr.find("td")]
    if not data_rows:
        return f"No regrade requests for assignment `{assignment_id}`."

    lines = [f"## Regrade Requests — Assignment {assignment_id}\n"]
    lines.append("| # | Status | Student | Question | Grader | Review Link |")
    lines.append("|---|--------|---------|----------|--------|-------------|")

    pending_count = 0
    completed_count = 0

    for i, row in enumerate(data_rows, 1):
        cells = row.find_all("td")
        cell_text = [c.get_text(strip=True) for c in cells]

        def _at(idx: int | None, fallback: int) -> str:
            if idx is not None and idx < len(cell_text):
                return cell_text[idx]
            if fallback < len(cell_text):
                return cell_text[fallback]
            return ""

        student = _at(student_idx, 0)
        question = _at(question_idx, 2)
        grader = _at(grader_idx, 3)
        completed_cell = _at(completed_idx, 4)

        # A regrade is "completed" only when the cell holds a real value
        # (typically a date/time stamp). "—", "Pending", "" and similar
        # placeholders previously triggered the success badge — they don't now.
        normalized = completed_cell.strip().lower()
        is_completed = bool(normalized) and normalized not in _REGRADE_PLACEHOLDERS

        review_link = ""
        for a in row.find_all("a"):
            href = a.get("href", "")
            if "/grade" in href:
                review_link = href
                break

        qid_match = re.search(r"/questions/(\d+)", review_link)
        sid_match = re.search(r"/submissions/(\d+)", review_link)
        qid = qid_match.group(1) if qid_match else ""
        sid = sid_match.group(1) if sid_match else ""

        status = "✅" if is_completed else "⏳"
        if is_completed:
            completed_count += 1
        else:
            pending_count += 1

        # Surface the source cell when the IDs cannot be parsed so the user
        # can investigate instead of getting a silently empty Review Link.
        if qid:
            id_info = f"qid={qid}, sid={sid}"
        elif review_link:
            id_info = f"⚠ link parse failed: {review_link}"
        else:
            id_info = "⚠ no /grade link in row"

        lines.append(
            f"| {i} | {status} | {student} | {question} | {grader} | {id_info} |"
        )

    lines.append("")
    lines.append(f"**Pending:** {pending_count} | **Completed:** {completed_count} | **Total:** {pending_count + completed_count}")
    lines.append("")
    lines.append(
        "_Use `get_regrade_detail(course_id, question_id, submission_id)` to see "
        "the student's message and rubric for a specific request._"
    )

    return "\n".join(lines)


def get_regrade_detail(course_id: str, question_id: str, submission_id: str) -> str:
    """Get detailed information about a specific regrade request.

    Shows the student's regrade message, the current rubric, applied rubric items,
    the grader's response (if any), and links to the student's submission.
    Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID (from regrade request listing).
        submission_id: The submission ID (from regrade request listing).
    """
    if not course_id or not question_id or not submission_id:
        return "Error: course_id, question_id, and submission_id are required."

    try:
        conn = get_connection()
        url = (
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/questions/{question_id}/submissions/{submission_id}/grade"
        )
        resp = conn.session.get(url)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching regrade detail: {e}"

    if resp.status_code != 200:
        return f"Error: Cannot access grading page (status {resp.status_code})."

    soup = BeautifulSoup(resp.text, "html.parser")
    grader = soup.find(attrs={"data-react-class": "SubmissionGrader"})
    if not grader:
        return "Error: SubmissionGrader component not found."

    try:
        props = json.loads(grader.get("data-react-props", "{}"))
    except json.JSONDecodeError:
        return "Error: Could not parse grading data."

    # Extract question info
    question = props.get("question", {})
    q_title = question.get("title", "Unknown")
    q_weight = question.get("weight", "?")

    lines = [f"## Regrade Detail — Question {q_title} (max {q_weight} pts)\n"]

    # Assignment info
    assignment = props.get("assignment", {})
    if assignment.get("title"):
        lines.append(f"**Assignment:** {assignment.get('title')}")

    # Submission info
    sub = props.get("assignment_submission", {})
    if sub.get("score") is not None:
        lines.append(f"**Total assignment score:** {sub.get('score')}")

    lines.append("")

    # Current rubric
    rubric_items = props.get("rubric_items", [])
    evaluations = props.get("rubric_item_evaluations", [])
    applied_ids = {e["rubric_item_id"] for e in evaluations if e.get("present")}

    if rubric_items:
        lines.append("### Rubric Items")
        lines.append("| Applied | Description | Points |")
        lines.append("|---------|-------------|--------|")
        for ri in rubric_items:
            applied = "✅" if ri["id"] in applied_ids else "—"
            lines.append(f"| {applied} | {ri['description']} | {ri['weight']} |")
        lines.append("")

    # Open regrade request
    open_req = props.get("open_request")
    if open_req:
        lines.append("### ⏳ Open Regrade Request")
        lines.append(f"**Date:** {open_req.get('created_at', 'N/A')}")
        lines.append(f"\n**Student says:**\n> {open_req.get('student_comment', '(no message)')}\n")
        if open_req.get("staff_comment"):
            lines.append(f"**Staff response:**\n> {open_req.get('staff_comment')}\n")

    # Closed regrade requests
    closed = props.get("closed_requests", [])
    if closed:
        lines.append("### Closed Regrade Request(s)")
        for req in closed:
            lines.append(f"**Date:** {req.get('created_at', 'N/A')}")
            lines.append(f"\n**Student says:**\n> {req.get('student_comment', '(no message)')}\n")
            if req.get("staff_comment"):
                lines.append(f"**Staff response:**\n> {req.get('staff_comment')}\n")

    if not open_req and not closed:
        lines.append("_No regrade requests found for this submission._")

    # Submission PDF/page link
    pdf_url = props.get("pdf_url")
    if pdf_url:
        lines.append(f"\n**Submission page:** [View]({pdf_url})")

    # Page images for scanned exams
    files = props.get("files", [])
    if files:
        lines.append(f"\n### Submission Pages ({len(files)} files)")
        for f in files[:5]:
            if isinstance(f, dict) and f.get("url"):
                lines.append(f"- [Page]({f['url']})")
            elif isinstance(f, str):
                lines.append(f"- [Page]({f})")

    return "\n".join(lines)
