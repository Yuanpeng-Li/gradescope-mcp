"""Submission-related MCP tools."""

import contextlib
import pathlib
import re

from gradescopeapi.classes.upload import upload_assignment

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.grading import get_student_submission_content
from gradescope_mcp.tools.safety import write_confirmation_required


def upload_submission(
    course_id: str,
    assignment_id: str,
    file_paths: list[str],
    leaderboard_name: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Upload files as a submission to a Gradescope assignment.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        file_paths: List of absolute file paths to upload.
        leaderboard_name: Optional leaderboard display name.
        confirm_write: Must be True to perform the upload.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    if not file_paths:
        return "Error: at least one file path is required."

    # Validate file paths
    validated_paths = []
    for fp in file_paths:
        original_path = pathlib.Path(fp)
        if not original_path.is_absolute():
            return f"Error: file path must be absolute: {fp}"

        path = original_path.resolve()

        if not path.exists():
            return f"Error: file not found: {fp}"

        if not path.is_file():
            return f"Error: not a file: {fp}"

        validated_paths.append(path)

    if not confirm_write:
        details = [
            f"course_id=`{course_id}`",
            f"assignment_id=`{assignment_id}`",
            f"files={', '.join(str(path) for path in validated_paths)}",
        ]
        if leaderboard_name:
            details.append(f"leaderboard_name={leaderboard_name}")
        return write_confirmation_required("upload_submission", details)

    try:
        conn = get_connection()
        # ExitStack guarantees every successfully-opened handle is closed even
        # if a later open() raises (EISDIR, EACCES, race-deleted file). The
        # earlier try/finally only protected handles after the loop completed.
        # The arguments are also passed positionally — `upload_assignment`'s
        # signature is (session, course_id, assignment_id, *files, ...) so
        # mixing kw-args with *file_handles raised TypeError.
        with contextlib.ExitStack() as stack:
            file_handles = [
                stack.enter_context(open(path, "rb")) for path in validated_paths
            ]
            result_url = upload_assignment(
                conn.session,
                course_id,
                assignment_id,
                *file_handles,
                leaderboard_name=leaderboard_name,
            )

    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error uploading submission: {e}"

    if result_url:
        filenames = [p.name for p in validated_paths]
        return (
            f"✅ Submission uploaded successfully!\n"
            f"- **Files:** {', '.join(filenames)}\n"
            f"- **Submission URL:** {result_url}"
        )
    else:
        return (
            "❌ Upload failed. Possible reasons:\n"
            "- Assignment is past the due date\n"
            "- You don't have permission to submit\n"
            "- Invalid course or assignment ID"
        )


def get_assignment_submissions(course_id: str, assignment_id: str) -> str:
    """Get all submissions for an assignment (instructor/TA only).

    Works for all assignment types: scanned PDF, online, and code assignments.
    Returns submission IDs, graded status, and grading progress.

    Note: The returned IDs are **Global Submission IDs** (the whole assignment
    submission). For grading a specific question, you may need the per-question
    submission ID from `get_submission_grading_context`.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        conn = get_connection()
        # Primary: submissions.json (works for scanned PDF/image assignments)
        resp = conn.session.get(
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/assignments/{assignment_id}/submissions.json",
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        if resp.status_code == 200:
            return _format_submissions_json(resp.json(), assignment_id, course_id)

        # Fallback: scrape review_grades HTML table (works for online assignments)
        return _get_submissions_from_review_grades(conn, course_id, assignment_id)

    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching submissions: {e}"


def _format_submissions_json(data: dict, assignment_id: str, course_id: str) -> str:
    """Format submission data from the submissions.json endpoint."""
    detailed = data.get("detailed_submissions", {})
    basic = data.get("submissions", {})

    if not detailed and not basic:
        return f"No submissions found for assignment `{assignment_id}` in course `{course_id}`."

    subs = detailed or basic
    total = len(subs)
    graded = sum(1 for s in subs.values() if s.get("graded"))

    lines = [f"## Submissions for Assignment {assignment_id}\n"]
    lines.append(f"**Total submissions:** {total}")
    lines.append(f"**Graded:** {graded}/{total}\n")
    lines.append("| # | Global Submission ID | Graded | Progress | Late |")
    lines.append("|---|---------------|--------|----------|------|")

    for i, (sub_id, sub) in enumerate(sorted(subs.items(), key=lambda x: x[0]), 1):
        is_graded = "✅" if sub.get("graded") else "—"
        progress = sub.get("grading_progress")
        progress_str = f"{progress:.0f}%" if progress is not None else "—"
        late = "⚠️" if sub.get("late") else ""
        lines.append(f"| {i} | `{sub_id}` | {is_graded} | {progress_str} | {late} |")

    return "\n".join(lines)


_REVIEW_GRADES_PLACEHOLDERS = frozenset({"", "-", "--", "—", "–", "n/a"})
_REVIEW_GRADES_AFFIRMATIVE = frozenset({"yes", "y", "true", "graded", "done", "✓", "✅"})
_REVIEW_GRADES_SCORE_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?|Graded|✓|✅)\s*$"
)


def _get_submissions_from_review_grades(
    conn, course_id: str, assignment_id: str
) -> str:
    """Fallback: scrape submission list from the review_grades HTML table.

    Used for online assignments where submissions.json returns 404. Uses
    header-aware column resolution so that adding/removing the Sections column
    (or any other layout shift) doesn't pull score/graded data from the wrong
    cells, which used to silently corrupt the output.
    """
    from bs4 import BeautifulSoup

    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/assignments/{assignment_id}/review_grades"
    )
    resp = conn.session.get(url)
    if resp.status_code != 200:
        return f"Error: Cannot access submissions or review_grades (status {resp.status_code})."

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        return (
            f"Error: No submission data found for assignment `{assignment_id}`. "
            "The submissions.json endpoint returned 404 and the review_grades page "
            "has no table. This assignment type may not be supported yet."
        )

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

    score_idx = _col("score", "total score", "points")
    graded_idx = _col("graded?", "graded", "status")

    body = table.find("tbody") or table
    data_rows = [tr for tr in body.find_all("tr") if tr.find("td")]
    if not data_rows:
        return f"No submissions found for assignment `{assignment_id}` in course `{course_id}`."

    sub_id_pattern = re.compile(r"/submissions/(\d+)")
    submissions = []
    for row in data_rows:
        cells = row.find_all("td")
        cell_text = [c.get_text(strip=True) for c in cells]
        if not cell_text:
            continue

        sub_id = None
        for link in row.find_all("a", href=True):
            match = sub_id_pattern.search(link["href"])
            if match:
                sub_id = match.group(1)
                break
        if not sub_id:
            continue

        # Score: prefer the resolved column; fall back to the historical
        # cells[4] only when the header lookup fails.
        score_text = ""
        if score_idx is not None and score_idx < len(cell_text):
            score_text = cell_text[score_idx]
        elif len(cell_text) > 4:
            score_text = cell_text[4]

        # Graded: derived from the explicit column if present, otherwise from
        # the score column's content. ``--`` and other placeholders never
        # count, no matter which column they appear in.
        graded = False
        if graded_idx is not None and graded_idx < len(cell_text):
            flag = cell_text[graded_idx].strip().lower()
            if flag in _REVIEW_GRADES_AFFIRMATIVE:
                graded = True
        if not graded and score_text:
            cleaned = score_text.strip()
            if cleaned.lower() not in _REVIEW_GRADES_PLACEHOLDERS and _REVIEW_GRADES_SCORE_RE.match(cleaned):
                graded = True

        submissions.append({
            "id": sub_id,
            "score": score_text,
            "graded": graded,
        })

    total = len(submissions)
    graded = sum(1 for s in submissions if s["graded"])

    lines = [f"## Submissions for Assignment {assignment_id}\n"]
    lines.append(f"**Total submissions:** {total}")
    lines.append(f"**Graded:** {graded}/{total}")
    lines.append("_(Note: retrieved from review_grades fallback)_\n")
    lines.append("| # | Global Submission ID | Score | Graded |")
    lines.append("|---|---------------|-------|--------|")

    for i, sub in enumerate(submissions, 1):
        is_graded = "✅" if sub["graded"] else "—"
        lines.append(f"| {i} | `{sub['id']}` | {sub['score']} | {is_graded} |")

    return "\n".join(lines)


def get_student_submission(
    course_id: str, assignment_id: str, student_email: str
) -> str:
    """Get the full content of a specific student's submission.

    Requires instructor/TA access. Returns the student's text answers for each
    question, as well as direct URLs to any uploaded files or images.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_email: The student's email address.
    """
    if not course_id or not assignment_id or not student_email:
        return "Error: course_id, assignment_id, and student_email are required."

    return get_student_submission_content(course_id, assignment_id, student_email)


def get_assignment_graders(course_id: str, question_id: str) -> str:
    """Get the list of graders for a specific question (instructor/TA only).

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID within the assignment.
    """
    if not course_id or not question_id:
        return "Error: both course_id and question_id are required."

    try:
        conn = get_connection()
        graders = conn.account.get_assignment_graders(course_id, question_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching graders: {e}"

    if not graders:
        return f"No graders found for question `{question_id}` in course `{course_id}`."

    # Filter: some question types return raw user IDs or internal labels
    _DIRTY_GRADER_PATTERNS = {"(needs labeling)", "(none)", "(unassigned)"}
    named_graders = [
        g for g in graders
        if not str(g).isdigit()
        and str(g).lower().strip() not in _DIRTY_GRADER_PATTERNS
    ]
    id_only = [g for g in graders if str(g).isdigit()]
    dirty_labels = [
        g for g in graders
        if str(g).lower().strip() in _DIRTY_GRADER_PATTERNS
    ]

    lines = [
        f"## Graders for Question {question_id}\n",
        f"**Total graders:** {len(named_graders)}\n",
    ]
    for grader in sorted(named_graders):
        lines.append(f"- {grader}")

    if id_only or dirty_labels:
        extras_count = len(id_only) + len(dirty_labels)
        lines.append(
            f"\n⚠️ **Note:** {extras_count} system/auto-grader "
            f"{'entry was' if extras_count == 1 else 'entries were'} "
            "filtered from the list."
        )

    return "\n".join(lines)
