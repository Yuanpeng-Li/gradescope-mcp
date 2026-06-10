"""Grading and rubric-related MCP tools.

These tools provide read access to assignment outlines (question structure),
grade exports, and grading progress. They use endpoints discovered through
reverse engineering the Gradescope web application.
"""

import csv
import io
import json
import logging
import re
import statistics

from bs4 import BeautifulSoup

from gradescope_mcp.auth import get_connection, AuthError

logger = logging.getLogger(__name__)


def _get_outline_data(course_id: str, assignment_id: str) -> dict:
    """Fetch and parse outline React props from /outline/edit.

    Supports both AssignmentEditor (online assignments) and
    AssignmentOutline (scanned PDF exams) components.

    Returns the full props dict with questions, assignment info, etc.
    Raises ValueError if the page structure is not as expected.
    """
    conn = get_connection()
    url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/outline/edit"
    resp = conn.session.get(url)

    if resp.status_code != 200:
        raise ValueError(
            f"Cannot access assignment outline (status {resp.status_code}). "
            "Check course_id, assignment_id, and your permissions."
        )

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try AssignmentEditor first (online/homework assignments)
    editor = soup.find(attrs={"data-react-class": "AssignmentEditor"})
    if editor is not None:
        return json.loads(editor.get("data-react-props", "{}"))

    # Fallback: AssignmentOutline (scanned PDF exams)
    outline_tag = soup.find(attrs={"data-react-class": "AssignmentOutline"})
    if outline_tag is not None:
        props = json.loads(outline_tag.get("data-react-props", "{}"))
        # Normalize: AssignmentOutline stores data in "outline" (list)
        # and "assignment" (dict). Convert outline list → questions dict
        # to match the format _build_question_tree expects.
        outline_list = props.get("outline", [])
        questions = {}

        def _flatten(items, parent_id=None):
            # AssignmentOutline encodes parenthood via the children list, not
            # via a per-item parent_id field. Without threading the parent_id
            # through the recursion, every question would land at the top
            # level and _build_question_tree would lose the grouping.
            for item in items:
                qid = str(item["id"])
                questions[qid] = {
                    "id": item["id"],
                    "type": item.get("type", ""),
                    "title": item.get("title", ""),
                    "weight": item.get("weight"),
                    "index": item.get("index", 0),
                    "parent_id": item.get("parent_id", parent_id),
                    "content": item.get("content", []),
                }
                children = item.get("children", [])
                if children:
                    _flatten(children, parent_id=item["id"])

        _flatten(outline_list)
        props["questions"] = questions
        return props

    raise ValueError(
        "Neither AssignmentEditor nor AssignmentOutline component found. "
        "The assignment type may not be supported."
    )


def _build_question_tree(questions: dict) -> list[dict]:
    """Organize flat question dict into a parent→children tree.

    Returns a list of top-level question groups, each with a 'children' list.
    """
    by_id = {}
    roots = []

    for qid, q in questions.items():
        node = {
            "id": q.get("id"),
            "type": q.get("type"),
            "title": q.get("title") or "",
            "weight": q.get("weight"),
            "index": q.get("index", 0),
            "parent_id": q.get("parent_id"),
            "content": q.get("content", []),
            "children": [],
        }
        by_id[node["id"]] = node

    for node in by_id.values():
        parent_id = node["parent_id"]
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)

    # Sort by index
    roots.sort(key=lambda x: x["index"])
    for r in roots:
        r["children"].sort(key=lambda x: x["index"])

    return roots


def _extract_text_content(content_list: list) -> str:
    """Extract readable text from question content blocks."""
    parts = []
    for item in content_list:
        if item.get("type") == "text":
            parts.append(item.get("value", ""))
        elif item.get("type") == "explanation":
            val = item.get("value", "")
            if val:
                parts.append(f"[Answer key: {val[:100]}{'...' if len(val) > 100 else ''}]")
    return " ".join(parts).strip()


def get_assignment_outline(course_id: str, assignment_id: str) -> str:
    """Get the question/rubric outline for an assignment.

    Returns the hierarchical question structure with IDs, types, weights,
    and question text. This is the foundation for rubric creation and grading.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        props = _get_outline_data(course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching outline: {e}"

    questions = props.get("questions", {})
    if not questions:
        return f"No questions found for assignment `{assignment_id}`."

    tree = _build_question_tree(questions)

    # Format output
    assignment_info = props.get("assignment", {})
    lines = [f"## Assignment Outline\n"]

    if assignment_info:
        atype = assignment_info.get("type", "Unknown")
        lines.append(f"**Type:** {atype}")

    lines.append(f"**Total questions:** {len(questions)}\n")

    group_num = 0
    for group in tree:
        group_num += 1
        group_title = group["title"] or f"Question Group {group_num}"
        group_weight = group["weight"]
        lines.append(f"### {group_title} ({group_weight} pts)\n")

        if group["children"]:
            lines.append("| # | Question ID | Weight | Type | Question Text |")
            lines.append("|---|-------------|--------|------|---------------|")
            for i, child in enumerate(group["children"], 1):
                text = _extract_text_content(child["content"])
                # Truncate for table display
                if len(text) > 120:
                    text = text[:117] + "..."
                # Escape pipes in text
                text = text.replace("|", "\\|")
                lines.append(
                    f"| {group_num}.{i} | `{child['id']}` | {child['weight']} | "
                    f"{child['type']} | {text} |"
                )
            lines.append("")
        else:
            # It's a standalone question (no children)
            if group["id"]:
                lines.append(f"**Question ID:** `{group['id']}`")
            text = _extract_text_content(group["content"])
            if text:
                lines.append(f"_{text[:200]}_\n")
            else:
                lines.append("")

    return "\n".join(lines)


def _fetch_assignment_scores_csv(
    course_id: str, assignment_id: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Fetch and parse the /scores CSV for an assignment.

    Returns ``(rows, fieldnames)``. Raises ``AuthError`` for auth failures
    and ``ValueError`` for HTTP / content-type problems so callers can
    return well-formatted error strings.
    """
    conn = get_connection()
    url = (
        f"{conn.gradescope_base_url}"
        f"/courses/{course_id}/assignments/{assignment_id}/scores"
    )
    resp = conn.session.get(url)
    if resp.status_code != 200:
        raise ValueError(
            f"Cannot access scores (status {resp.status_code}). "
            "Check permissions."
        )
    content_type = resp.headers.get("content-type", "")
    if "csv" not in content_type and "text" not in content_type:
        raise ValueError(f"Unexpected content type: {content_type}")

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def get_student_assignment_link(
    course_id: str,
    assignment_id: str,
    student_name: str,
) -> str:
    """Return the per-student `/assignments/.../submissions/Z` URL.

    Looks up the student's row in the scores CSV by exact-match name and
    returns the Gradescope link that opens the **entire** assignment
    submission (cover sheet view), not a single-question grade page.

    Useful for skim-review of one student across all questions, e.g. when
    flagged by an outlier-detection pass.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_name: Exact match against ``"First Last"`` as Gradescope
            stores it (concatenation of First Name + Last Name columns).
            Case-sensitive.

    Returns:
        The plain submission URL as a single-line string on success, or
        a clear ``Error: ...`` message if the student isn't found, has no
        submission, or matches multiple rows.
    """
    if not course_id or not assignment_id or not student_name:
        return "Error: course_id, assignment_id, and student_name are required."

    try:
        rows, _fields = _fetch_assignment_scores_csv(course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching scores: {e}"

    conn = get_connection()

    needle = student_name.strip()
    matches = []
    for r in rows:
        full = f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip()
        if full == needle:
            matches.append(r)

    if not matches:
        return (
            f"Error: no student named `{needle}` found in assignment "
            f"`{assignment_id}` roster. Check spelling — match is "
            f"case-sensitive against 'First Last' as it appears in the "
            f"scores CSV."
        )
    if len(matches) > 1:
        # Two students with literally identical names — surface them.
        emails = [r.get("Email", "") for r in matches]
        return (
            f"Error: multiple students named `{needle}` found "
            f"({len(matches)} rows). Disambiguate by email: {emails}."
        )

    r = matches[0]
    sub_id = (r.get("Submission ID") or "").strip()
    if not sub_id:
        return (
            f"Student `{needle}` has no submission for assignment "
            f"`{assignment_id}` (missing / not submitted)."
        )

    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/assignments/{assignment_id}/submissions/{sub_id}"
    )
    return f"{url}"


# Columns that aren't per-question score columns.
_NON_QUESTION_CSV_COLUMNS = frozenset({
    "First Name", "Last Name", "SID", "Email", "Sections",
    "Total Score", "Max Points", "Status", "Submission ID",
    "Submission Time", "Lateness (H:M:S)", "View Count",
    "Submission Count",
})


def _row_max_points(r: dict) -> str:
    return r.get("Max Points", "N/A") or "N/A"


def _parse_score(value) -> float | None:
    """Best-effort float-parse a CSV cell. Returns None for blanks/junk."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _summarize_scores_csv(rows: list[dict], fieldnames: list[str]) -> dict:
    """Compute summary statistics + per-question column list from CSV rows."""
    graded = [r for r in rows if r.get("Status") == "Graded"]
    missing = [r for r in rows if r.get("Status") == "Missing"]
    # Submitted = not missing AND has a non-empty submission marker. Many
    # assignments leave Status blank for "submitted but not yet graded", so
    # rely on Submission ID / Submission Time as positive evidence rather than
    # treating blank Status as ungraded.
    submitted = [
        r for r in rows
        if r.get("Status") != "Missing"
        and (r.get("Submission ID") or r.get("Submission Time"))
    ]

    scores: list[float] = []
    for r in graded:
        parsed = _parse_score(r.get("Total Score"))
        if parsed is not None:
            scores.append(parsed)

    distinct_max = {_row_max_points(r) for r in rows}
    summary_max = next(iter(distinct_max)) if len(distinct_max) == 1 else "varies"

    question_cols = [c for c in fieldnames if c not in _NON_QUESTION_CSV_COLUMNS]

    summary = {
        "total_students": len(rows),
        "graded": len(graded),
        "submitted_not_yet_graded": max(0, len(submitted) - len(graded)),
        "missing": len(missing),
        "max_points": summary_max,
        "question_columns": question_cols,
    }
    if scores:
        summary["average_score"] = round(statistics.mean(scores), 2)
        summary["min_score"] = min(scores)
        summary["max_score"] = max(scores)
        summary["median_score"] = statistics.median(scores)
    return summary


def export_assignment_scores(
    course_id: str,
    assignment_id: str,
    output_format: str = "markdown",
) -> str:
    """Export per-question scores for an assignment.

    Returns either a markdown table (truncated to the first 20 students)
    for quick eyeballing, or a complete JSON payload with every student
    and every per-question score. Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        output_format: ``"markdown"`` (default) for a compact summary
            table, or ``"json"`` for the full, untruncated payload
            including per-question scores per student. Use ``"json"``
            when you need every row or need to feed scores into other
            analysis.

    Returns:
        Markdown string when ``output_format="markdown"`` (default),
        or a JSON string when ``output_format="json"``. The JSON shape
        is ``{"assignment_id", "summary", "students": [{"name", "email",
        "sid", "submission_id", "status", "total_score", "max_points",
        "lateness", "question_scores": {col: float|None, ...}}, ...]}``.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    if output_format not in ("markdown", "json"):
        return 'Error: output_format must be "markdown" or "json".'

    try:
        rows, fieldnames = _fetch_assignment_scores_csv(course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching scores: {e}"

    if not rows:
        if output_format == "json":
            return json.dumps({
                "assignment_id": assignment_id,
                "summary": {"total_students": 0},
                "students": [],
            })
        return f"No scores found for assignment `{assignment_id}`."

    summary = _summarize_scores_csv(rows, fieldnames)
    question_cols = summary["question_columns"]

    if output_format == "json":
        students = []
        for r in rows:
            name = f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip()
            students.append({
                "name": name,
                "email": r.get("Email", ""),
                "sid": r.get("SID", ""),
                "submission_id": r.get("Submission ID", ""),
                "status": r.get("Status", ""),
                "total_score": _parse_score(r.get("Total Score")),
                "max_points": _row_max_points(r),
                "lateness": r.get("Lateness (H:M:S)", ""),
                "question_scores": {
                    col: _parse_score(r.get(col)) for col in question_cols
                },
            })
        return json.dumps({
            "assignment_id": assignment_id,
            "summary": summary,
            "students": students,
        }, indent=2)

    # Markdown summary (existing behavior, lightly refactored)
    lines = [f"## Scores for Assignment {assignment_id}\n"]
    lines.append(f"**Total students:** {summary['total_students']}")
    lines.append(f"**Graded:** {summary['graded']}")
    lines.append(f"**Submitted (not yet graded):** {summary['submitted_not_yet_graded']}")
    lines.append(f"**Missing:** {summary['missing']}")
    lines.append(f"**Max points:** {summary['max_points']}")

    if "average_score" in summary:
        lines.append(f"**Average score:** {summary['average_score']:.2f}")
        lines.append(
            f"**Min:** {summary['min_score']:.1f} | "
            f"**Max:** {summary['max_score']:.1f}"
        )
        lines.append(f"**Median:** {summary['median_score']:.1f}")

    if question_cols:
        lines.append(f"\n**Question breakdown:** {', '.join(question_cols)}")

    lines.append(
        f"\n### Student Scores (showing {min(20, len(rows))} of {len(rows)})\n"
    )
    lines.append("| Name | Email | Total | Status | Lateness |")
    lines.append("|------|-------|-------|--------|----------|")
    for r in rows[:20]:
        name = f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip()
        email = r.get("Email", "")
        total = r.get("Total Score", "N/A")
        status = r.get("Status", "N/A")
        lateness = r.get("Lateness (H:M:S)", "")
        lines.append(
            f"| {name} | {email} | {total}/{_row_max_points(r)} | {status} | {lateness} |"
        )

    if len(rows) > 20:
        lines.append(
            f"\n_... and {len(rows) - 20} more students. "
            f"Re-run with `output_format=\"json\"` to get all rows._"
        )

    return "\n".join(lines)


def get_grading_progress(course_id: str, assignment_id: str) -> str:
    """Get the grading progress dashboard for an assignment.

    Shows each question's grading status: how many submissions have been graded,
    assigned graders, and completion percentage. Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        conn = get_connection()
        url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/grade.json"
        resp = conn.session.get(url)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching grading progress: {e}"

    if resp.status_code != 200:
        return f"Error: Cannot access grading dashboard (status {resp.status_code})."

    try:
        data = resp.json()
    except Exception:
        return "Error: Failed to parse grading dashboard response."

    # Extract assignment data
    assignments = data.get("assignments", {})
    if not assignments:
        return f"No grading data found for assignment `{assignment_id}`."

    # assignments can be dict or list
    if isinstance(assignments, dict):
        assignment_data = assignments.get(assignment_id, {})
        if not assignment_data:
            # Try first value
            assignment_data = next(iter(assignments.values()), {})
    elif isinstance(assignments, list):
        assignment_data = assignments[0] if assignments else {}
    else:
        assignment_data = {}

    questions = assignment_data.get("questions", {})
    if not questions:
        return f"No questions found in grading dashboard for assignment `{assignment_id}`."

    lines = [f"## Grading Progress — Assignment {assignment_id}\n"]

    # Build question tree for nice formatting
    total_graded = 0
    total_count = 0

    lines.append("| Question | Type | Graded | Total | Progress | Graders |")
    lines.append("|----------|------|--------|-------|----------|---------|")

    questions_by_id = {q.get("id"): q for q in questions.values()}
    children_by_parent = {}
    roots = []

    for q in questions.values():
        parent_id = q.get("parent_id")
        if parent_id and parent_id in questions_by_id:
            children_by_parent.setdefault(parent_id, []).append(q)
        else:
            roots.append(q)

    def _question_number(q: dict, fallback: int) -> int:
        index = q.get("index")
        try:
            return int(index)
        except (TypeError, ValueError):
            return fallback

    for root_fallback, root in enumerate(sorted(roots, key=lambda x: x.get("index", 0)), 1):
        root_number = _question_number(root, root_fallback)
        children = sorted(children_by_parent.get(root.get("id"), []), key=lambda x: x.get("index", 0))

        if root.get("question_group"):
            for child_fallback, child in enumerate(children, 1):
                child_number = _question_number(child, child_fallback)
                graded = child.get("total_graded_count", 0)
                count = child.get("total_count", 0)
                total_graded += graded
                total_count += count
                pct = f"{graded / count * 100:.0f}%" if count > 0 else "N/A"
                graders = ", ".join(g.get("name", "?") for g in child.get("graders", []))
                child_title = child.get("title", "")
                label = f"Q{root_number}.{child_number}"
                if child_title:
                    label += f" {child_title}"
                qtype = child.get("type", "")
                lines.append(
                    f"| {label} (`{child['id']}`) | {qtype} | {graded} | {count} | {pct} | {graders or 'Unassigned'} |"
                )
            continue

        graded_count = root.get("total_graded_count", 0)
        count = root.get("total_count", 0)
        total_graded += graded_count
        total_count += count
        pct = f"{graded_count / count * 100:.0f}%" if count > 0 else "N/A"
        graders = ", ".join(g.get("name", "?") for g in root.get("graders", []))
        root_title = root.get("title", "")
        label = f"Q{root_number}"
        if root_title:
            label += f" {root_title}"
        lines.append(
            f"| {label} (`{root['id']}`) | {root.get('type', '')} | {graded_count} | {count} | {pct} | {graders or 'Unassigned'} |"
        )

        for child_fallback, child in enumerate(children, 1):
            child_number = _question_number(child, child_fallback)
            graded = child.get("total_graded_count", 0)
            count = child.get("total_count", 0)
            total_graded += graded
            total_count += count
            pct = f"{graded / count * 100:.0f}%" if count > 0 else "N/A"
            graders = ", ".join(g.get("name", "?") for g in child.get("graders", []))
            child_title = child.get("title", "")
            label = f"Q{root_number}.{child_number}"
            if child_title:
                label += f" {child_title}"
            qtype = child.get("type", "")
            lines.append(
                f"| {label} (`{child['id']}`) | {qtype} | {graded} | {count} | {pct} | {graders or 'Unassigned'} |"
            )

    lines.append("")

    # Summary
    if total_count > 0:
        overall_pct = total_graded / total_count * 100
        lines.append(f"**Overall progress:** {total_graded}/{total_count} ({overall_pct:.0f}%)")

    # Action button
    action = data.get("action_button", {})
    if action:
        lines.append(f"\n**Next step:** [{action.get('text', 'Continue')}]({action.get('link', '')})")

    return "\n".join(lines)


def _extract_scanned_pdf_content(html_text: str, student_name: str,
                                  student_email: str, sub_id: str) -> str:
    """Extract page images from scanned PDF exam submissions.

    Scanned PDF exams do not use React components. Instead, page image data is
    embedded as JSON in the raw HTML source. This function extracts page image
    URLs, the full PDF URL, and question-to-page crop mappings.
    """
    lines = [f"## Submission Content: {student_name} ({student_email})"]
    lines.append(f"**Submission ID:** `{sub_id}`")
    lines.append("**Format:** Scanned PDF Exam\n")

    # Extract full PDF URL
    pdf_match = re.search(
        r'"url":"(https://production-gradescope-uploads[^"]*?output\.pdf[^"]*?)"',
        html_text,
    )
    if pdf_match:
        pdf_url = pdf_match.group(1).encode("utf-8").decode("unicode_escape")
        lines.append(f"**Full PDF:** [Download]({pdf_url})\n")

    # Extract individual page images
    page_data = []
    for m in re.finditer(
        r'"number":(\d+),"width":(\d+),"height":(\d+),"url":"(.*?)"',
        html_text,
    ):
        num = int(m.group(1))
        w, h = int(m.group(2)), int(m.group(3))
        url = m.group(4).encode("utf-8").decode("unicode_escape")
        page_data.append((num, w, h, url))

    # Deduplicate by page number (keep first occurrence)
    seen = set()
    unique_pages = []
    for p in sorted(page_data):
        if p[0] not in seen:
            seen.add(p[0])
            unique_pages.append(p)

    if unique_pages:
        lines.append(f"### Scanned Pages ({len(unique_pages)} pages)\n")
        for num, w, h, url in unique_pages:
            lines.append(f"- **Page {num}** ({w}×{h}): [View Image]({url})")
    else:
        lines.append("No scanned page images found.")

    # Try to extract score from the page
    score_match = re.search(r'"score":"?([0-9.]+)"?', html_text)
    if score_match:
        lines.insert(2, f"**Total Score:** {score_match.group(1)} points")

    return "\n".join(lines)


def get_student_submission_content(course_id: str, assignment_id: str,
                                    student_email: str) -> str:
    """Get the full content of a student's submission, including text answers and image URLs.

    Supports two submission formats:
    1. **Online assignments**: Extracts text answers and uploaded file URLs from
       the AssignmentSubmissionViewer React component.
    2. **Scanned PDF exams**: Extracts per-page scanned images and the full PDF
       from embedded JSON in the raw HTML.

    Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_email: The student's email address.
    """
    if not course_id or not assignment_id or not student_email:
        return "Error: course_id, assignment_id, and student_email are required."

    try:
        conn = get_connection()
        # First, find the submission ID for the student
        url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/scores"
        resp = conn.session.get(url)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching scores to find submission: {e}"

    if resp.status_code != 200:
        return f"Error accessing scores (status {resp.status_code})."

    # Find the student's submission ID
    reader = csv.DictReader(io.StringIO(resp.text))
    sub_id = None
    student_name = student_email
    for row in reader:
        if row.get("Email") == student_email:
            if row.get("Status") == "Missing" or not row.get("Submission ID"):
                return f"Student {student_email} has no submission for this assignment."
            sub_id = row.get("Submission ID")
            student_name = f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
            break

    if not sub_id:
        return f"Could not find student {student_email} in the course roster/scores."

    # Fetch the submission page
    try:
        sub_url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/submissions/{sub_id}"
        resp2 = conn.session.get(sub_url)
    except Exception as e:
        return f"Error fetching submission page: {e}"

    if resp2.status_code != 200:
        return f"Error accessing submission {sub_id} (status {resp2.status_code})."

    soup = BeautifulSoup(resp2.text, "html.parser")
    viewer = soup.find(attrs={"data-react-class": "AssignmentSubmissionViewer"})

    # ---- Path A: Online Assignment (has AssignmentSubmissionViewer) ----
    if viewer:
        return _extract_online_submission(viewer, student_name, student_email, sub_id)

    # ---- Path B: Scanned PDF Exam (no React component) ----
    # Check if the page contains embedded page image data
    if "production-gradescope-uploads" in resp2.text and '"number":' in resp2.text:
        return _extract_scanned_pdf_content(
            resp2.text, student_name, student_email, sub_id
        )

    return (
        f"## Submission Content: {student_name} ({student_email})\n"
        f"**Submission ID:** `{sub_id}`\n\n"
        "Could not extract submission content. The assignment format "
        "may not be supported yet."
    )


def _extract_online_submission(viewer, student_name: str,
                                student_email: str, sub_id: str) -> str:
    """Extract content from an online assignment using AssignmentSubmissionViewer."""
    props_str = viewer.get("data-react-props", "{}")
    try:
        props = json.loads(props_str)
    except json.JSONDecodeError:
        return "Error: Could not parse submission data."

    # Extract files and answers
    text_files = props.get("text_files", [])
    file_map = {}
    for f in text_files:
        fid = f.get("id")
        furl = f.get("file", {}).get("url")
        if fid and furl:
            file_map[fid] = furl

    answers = {"questions": {}}
    q_subs = props.get("question_submissions", [])
    for qs in q_subs:
        qid = str(qs.get("question_id"))
        ans_data = qs.get("answers", {})

        # answers can be dict {'0': 'text'} or {'0': [{'text_file_id': 123}]}
        content = []
        for key, val in ans_data.items():
            if isinstance(val, str):
                content.append(val)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict) and "text_file_id" in item:
                        fid = item["text_file_id"]
                        if fid in file_map:
                            content.append(f"[Image/File URL: {file_map[fid]}]")
                    else:
                        content.append(str(item))

        # Also get current score if graded
        score = qs.get("score")

        answers["questions"][qid] = {
            "answer": "\n".join(content) if content else "(No answer provided)",
            "score": score,
        }

    # Format output
    lines = [f"## Submission Content: {student_name} ({student_email})"]
    lines.append(f"**Submission ID:** `{sub_id}`")

    meta = props.get("assignment_submission", {})
    if meta.get("score") is not None:
        lines.append(f"**Total Score:** {meta.get('score')} points")
    if meta.get("pdf_url"):
        lines.append(f"**Full Submission PDF:** [Link]({meta.get('pdf_url')})")

    lines.append("")

    if not answers["questions"]:
        lines.append("No question-specific answers found. This might be a PDF-only assignment.")
        # Print uploaded files anyway
        if file_map:
            lines.append("### Uploaded Files:")
            for fid, furl in file_map.items():
                lines.append(f"- [File {fid}]({furl})")
        return "\n".join(lines)

    for qid, data in answers["questions"].items():
        score_text = f" (Score: {data['score']})" if data["score"] is not None else ""
        lines.append(f"### Question `{qid}`{score_text}")
        lines.append(f"{data['answer']}\n")

    return "\n".join(lines)
