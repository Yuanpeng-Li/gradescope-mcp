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
from gradescope_mcp.tools.common import (
    escape_md_cell,
    format_untrusted,
    normalize_url,
)

logger = logging.getLogger(__name__)


def _sanitize_inline(value) -> str:
    """Make student-controlled text (e.g. a display name) safe on one line.

    Newlines and runs of whitespace collapse to single spaces, so the text
    cannot start its own markdown line (a heading or a fake instruction),
    and ``|`` is escaped so it cannot split a table cell. ``None`` renders as
    an empty string.
    """
    if value is None:
        return ""
    return " ".join(str(value).split()).replace("|", "\\|")


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


def _index_key(node: dict) -> float:
    index = node.get("index")
    return index if isinstance(index, (int, float)) else 0


def _build_question_tree(questions: dict) -> list[dict]:
    """Organize flat question dict into a parent→children tree.

    Returns a list of top-level question groups, each with a 'children' list.
    Children may have their own 'children' (deeper nesting); every level is
    sorted by index. IDs are matched as strings so int/str mixes still nest.
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
        by_id[str(node["id"])] = node

    for node in by_id.values():
        parent_id = node["parent_id"]
        parent = by_id.get(str(parent_id)) if parent_id else None
        if parent is not None and parent is not node:
            parent["children"].append(node)
        else:
            roots.append(node)

    def _sort(nodes: list[dict]) -> None:
        nodes.sort(key=_index_key)
        for n in nodes:
            _sort(n["children"])

    _sort(roots)
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


def _append_outline_rows(children: list[dict], prefix: str, lines: list[str]) -> None:
    """Render outline table rows for ``children`` and, recursively, their children."""
    for i, child in enumerate(children, 1):
        label = f"{prefix}.{i}"
        text = " ".join(_extract_text_content(child["content"]).split())
        # Truncate for table display
        if len(text) > 120:
            text = text[:117] + "..."
        # Escape pipes in text
        text = text.replace("|", "\\|")
        lines.append(
            f"| {label} | `{child['id']}` | {child['weight']} | "
            f"{child['type']} | {text} |"
        )
        if child["children"]:
            _append_outline_rows(child["children"], label, lines)


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
            _append_outline_rows(group["children"], str(group_num), lines)
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


# Content types the /scores export may plausibly use. HTML (a login or error
# page) is always rejected, whatever the header says.
_CSV_CONTENT_TYPES = (
    "csv", "text/plain", "application/octet-stream", "application/vnd.ms-excel",
)


def _decode_csv_body(resp, content_type: str) -> str:
    """Decode the scores export, preferring UTF-8 (with or without a BOM).

    requests decodes a charset-less ``text/csv`` body as ISO-8859-1, which
    mangles non-ASCII names, and a UTF-8 BOM would otherwise stay glued to
    the first header (so "First Name" would never match).
    """
    raw = getattr(resp, "content", None)
    declared = ""
    if "charset=" in content_type:
        declared = content_type.split("charset=", 1)[1].split(";")[0].strip().strip('"')
    if isinstance(raw, (bytes, bytearray)) and declared.replace("-", "") in ("", "utf8"):
        try:
            return bytes(raw).decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    return (resp.text or "").lstrip("\ufeff")


def _fetch_assignment_scores_csv(
    course_id: str, assignment_id: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Fetch and parse the /scores CSV for an assignment.

    Returns ``(rows, fieldnames)``. Raises ``AuthError`` for auth failures
    and ``ValueError`` for HTTP / content-type problems (including an HTML
    page where the CSV was expected) so callers can return well-formatted
    error strings.
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
    content_type = (resp.headers.get("content-type") or "").lower()
    body = _decode_csv_body(resp, content_type)
    if "html" in content_type or body.lstrip().startswith("<"):
        raise ValueError(
            "Expected the scores CSV export but received an HTML page "
            f"(content-type: {content_type or 'none'}). The session may have "
            "expired, or you may lack access to this assignment's scores."
        )
    if not any(t in content_type for t in _CSV_CONTENT_TYPES):
        raise ValueError(
            f"Unexpected content type for the scores export: {content_type or 'none'}"
        )

    reader = csv.DictReader(io.StringIO(body))
    rows = list(reader)
    fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def _normalize_name(name: str) -> str:
    """Collapse runs of whitespace so 'Mary  Smith' and 'Mary ' + 'Smith' match."""
    return " ".join((name or "").split())


def _row_full_name(r: dict) -> str:
    return _normalize_name(f"{r.get('First Name') or ''} {r.get('Last Name') or ''}")


def _row_email(r: dict) -> str:
    return (r.get("Email") or "").strip()


def get_student_assignment_link(
    course_id: str,
    assignment_id: str,
    student_name: str,
    student_email: str | None = None,
) -> str:
    """Return the per-student `/assignments/.../submissions/Z` URL.

    Looks up the student's row in the scores CSV and returns the Gradescope
    link that opens the **entire** assignment submission (cover sheet view),
    not a single-question grade page.

    Useful for skim-review of one student across all questions, e.g. when
    flagged by an outlier-detection pass.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_name: ``"First Last"`` as Gradescope stores it (concatenation
            of First Name + Last Name columns). Case-sensitive; runs of
            whitespace are collapsed on both sides. May be empty when
            ``student_email`` is given.
        student_email: Optional, case-insensitive match against the Email
            column. Takes precedence over ``student_name``; use it to pick
            one of several students with the same name.

    Returns:
        The plain submission URL as a single-line string on success, or
        a clear ``Error: ...`` message if the student isn't found, has no
        submission, or matches multiple rows (every match is then listed
        with its email and link).
    """
    email_needle = (student_email or "").strip()
    name_needle = _normalize_name(student_name or "")
    if not course_id or not assignment_id or not (name_needle or email_needle):
        return (
            "Error: course_id, assignment_id, and student_name (or "
            "student_email) are required."
        )

    try:
        rows, _fields = _fetch_assignment_scores_csv(course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching scores: {e}"

    conn = get_connection()

    def _url(sub_id: str) -> str:
        return (
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/assignments/{assignment_id}/submissions/{sub_id}"
        )

    if email_needle:
        wanted = email_needle.casefold()
        matches = [r for r in rows if _row_email(r).casefold() == wanted]
        who = f"with email `{email_needle}`"
        if not matches:
            return (
                f"Error: no student {who} found in assignment "
                f"`{assignment_id}` roster (email match is case-insensitive)."
            )
    else:
        matches = [r for r in rows if _row_full_name(r) == name_needle]
        who = f"named `{name_needle}`"
        if not matches:
            return (
                f"Error: no student {who} found in assignment "
                f"`{assignment_id}` roster. Check spelling — match is "
                f"case-sensitive against 'First Last' as it appears in the "
                f"scores CSV — or pass student_email instead."
            )

    if len(matches) > 1:
        # Several rows match: list each with its email and link so the
        # caller can pick one (or re-run with student_email).
        options = []
        for r in matches:
            sub = (r.get("Submission ID") or "").strip()
            options.append(
                f"- {_sanitize_inline(_row_email(r)) or '(no email)'}: "
                f"{_url(sub) if sub else 'no submission'}"
            )
        return (
            f"Error: multiple students {who} found ({len(matches)} rows). "
            "Re-run with student_email set to one of these to pick one:\n"
            + "\n".join(options)
        )

    r = matches[0]
    sub_id = (r.get("Submission ID") or "").strip()
    if not sub_id:
        return (
            f"Student {who} has no submission for assignment "
            f"`{assignment_id}` (missing / not submitted)."
        )

    return _url(sub_id)


# Columns that aren't per-question score columns.
_NON_QUESTION_CSV_COLUMNS = frozenset({
    "First Name", "Last Name", "SID", "Email", "Sections",
    "Total Score", "Max Points", "Status", "Submission ID",
    "Submission Time", "Lateness (H:M:S)", "View Count",
    "Submission Count",
})


def _row_max_points(r: dict) -> str:
    return (r.get("Max Points") or "").strip() or "N/A"


def _row_total_display(r: dict) -> str:
    """'7.0/10.0' for a scored row, '—' when there is no total (e.g. Missing)."""
    total = (r.get("Total Score") or "").strip()
    max_points = (r.get("Max Points") or "").strip()
    if not total:
        return "—"
    return f"{total}/{max_points}" if max_points else total


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

    # Blank Max Points (e.g. on Missing rows) says nothing about the maximum,
    # so it must not turn a uniform maximum into "varies".
    distinct_max = {
        (r.get("Max Points") or "").strip() for r in rows
    } - {"", "N/A"}
    if not distinct_max:
        summary_max = "N/A"
    elif len(distinct_max) == 1:
        summary_max = next(iter(distinct_max))
    else:
        summary_max = "varies"

    question_cols = [c for c in fieldnames if c not in _NON_QUESTION_CSV_COLUMNS]
    not_yet_graded = max(0, len(submitted) - len(graded))

    # Every key is always present (None when there is nothing to compute) so
    # the JSON shape does not depend on the data.
    summary = {
        "total_students": len(rows),
        "graded": len(graded),
        "submitted_not_yet_graded": not_yet_graded,
        "missing": len(missing),
        "max_points": summary_max,
        "question_columns": question_cols,
        "scores_counted": len(scores),
        "score_basis": (
            f"over {len(scores)} fully graded submission(s) (Status 'Graded'); "
            f"excludes {len(missing)} missing and {not_yet_graded} not yet graded"
        ),
        "average_score": None,
        "min_score": None,
        "max_score": None,
        "median_score": None,
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
                "summary": _summarize_scores_csv([], fieldnames),
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

    if summary["average_score"] is not None:
        lines.append(f"**Score statistics:** {summary['score_basis']}")
        lines.append(f"**Average score:** {summary['average_score']:.2f}")
        lines.append(
            f"**Min:** {summary['min_score']:.1f} | "
            f"**Max:** {summary['max_score']:.1f}"
        )
        lines.append(f"**Median:** {summary['median_score']:.1f}")
    else:
        lines.append("**Score statistics:** none yet (no fully graded submissions)")

    if question_cols:
        lines.append(f"\n**Question breakdown:** {', '.join(question_cols)}")

    lines.append(
        f"\n### Student Scores (showing {min(20, len(rows))} of {len(rows)})\n"
    )
    lines.append("| Name | Email | Total | Status | Lateness |")
    lines.append("|------|-------|-------|--------|----------|")
    for r in rows[:20]:
        name = _row_full_name(r)
        email = _row_email(r)
        status = r.get("Status") or "N/A"
        lateness = r.get("Lateness (H:M:S)", "")
        lines.append(
            f"| {escape_md_cell(name)} | {escape_md_cell(email)} "
            f"| {escape_md_cell(_row_total_display(r))} | {escape_md_cell(status)} "
            f"| {escape_md_cell(lateness)} |"
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
    assignments = data.get("assignments") if isinstance(data, dict) else None
    if not assignments:
        return f"No grading data found for assignment `{assignment_id}`."

    # assignments can be dict or list. Never fall back to another entry: the
    # heading names the requested assignment, so borrowed data would be
    # silently mislabeled.
    assignment_data = None
    available: list[str] = []
    if isinstance(assignments, dict):
        available = [str(k) for k in assignments]
        assignment_data = assignments.get(str(assignment_id))
    elif isinstance(assignments, list):
        entries = [a for a in assignments if isinstance(a, dict)]
        available = [str(a.get("id")) for a in entries]
        assignment_data = next(
            (a for a in entries if str(a.get("id")) == str(assignment_id)), None
        )
    if not isinstance(assignment_data, dict):
        return (
            f"Error: the grading dashboard (grade.json) has no entry for "
            f"assignment `{assignment_id}` (entries found: {available[:10]}). "
            "Not showing another assignment's progress."
        )

    raw_questions = assignment_data.get("questions") or {}
    if isinstance(raw_questions, dict):
        question_list = list(raw_questions.values())
    elif isinstance(raw_questions, list):
        question_list = raw_questions
    else:
        question_list = []
    question_list = [q for q in question_list if isinstance(q, dict) and q.get("id") is not None]
    if not question_list:
        return f"No questions found in grading dashboard for assignment `{assignment_id}`."

    # One tree over every question, numbered in index order across groups and
    # standalone questions alike (the same numbering as the outline).
    nodes = {str(q["id"]): {"q": q, "children": []} for q in question_list}
    roots = []
    for node in nodes.values():
        parent_id = node["q"].get("parent_id")
        parent = nodes.get(str(parent_id)) if parent_id is not None else None
        if parent is not None and parent is not node:
            parent["children"].append(node)
        else:
            roots.append(node)

    def _sort(level: list[dict]) -> None:
        level.sort(key=lambda n: _index_key(n["q"]))
        for n in level:
            _sort(n["children"])

    _sort(roots)

    def _count(value) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def _leaf_totals(node: dict) -> tuple[int, int]:
        if not node["children"]:
            q = node["q"]
            return _count(q.get("total_graded_count")), _count(q.get("total_count"))
        graded = count = 0
        for child in node["children"]:
            g, c = _leaf_totals(child)
            graded += g
            count += c
        return graded, count

    def _pct(graded: int, count: int) -> str:
        return f"{graded / count * 100:.0f}%" if count > 0 else "N/A"

    lines = [f"## Grading Progress — Assignment {assignment_id}\n"]
    lines.append("| Question | Type | Graded | Total | Progress | Graders |")
    lines.append("|----------|------|--------|-------|----------|---------|")

    def _render(level: list[dict], prefix: str) -> None:
        for i, node in enumerate(level, 1):
            q = node["q"]
            label = f"{prefix}.{i}" if prefix else f"Q{i}"
            title = " ".join(str(q.get("title") or "").split())
            # Prefer Gradescope's own numbering when grade.json carries it.
            name = q.get("numbered_title") or (f"{label} {title}" if title else label)
            name = escape_md_cell(name)
            graded, count = _leaf_totals(node)
            if node["children"] or q.get("question_group"):
                lines.append(
                    f"| **{name}** (`{q['id']}`) | {escape_md_cell(q.get('type') or 'group')} "
                    f"| {graded} | {count} | {_pct(graded, count)} |  |"
                )
                _render(node["children"], label)
                continue
            graders = ", ".join(
                escape_md_cell(g.get("name", "?"))
                for g in (q.get("graders") or []) if isinstance(g, dict)
            )
            lines.append(
                f"| {name} (`{q['id']}`) | {escape_md_cell(q.get('type', ''))} | {graded} "
                f"| {count} | {_pct(graded, count)} | {graders or 'Unassigned'} |"
            )

    _render(roots, "")

    total_graded = total_count = 0
    for root in roots:
        g, c = _leaf_totals(root)
        total_graded += g
        total_count += c

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


def _csv_total_display(row: dict) -> str | None:
    """'7.0 / 10.0' from a scores-CSV row, or None when the row has no total."""
    total = (row.get("Total Score") or "").strip()
    if not total:
        return None
    max_points = (row.get("Max Points") or "").strip()
    return f"{total} / {max_points}" if max_points else total


def _extract_scanned_pdf_content(html_text: str, student_name: str,
                                  student_email: str, sub_id: str,
                                  csv_total: str | None = None) -> str:
    """Extract page images from scanned PDF exam submissions.

    Scanned PDF exams do not use React components. Instead, page image data is
    embedded as JSON in the raw HTML source. This function extracts page image
    URLs, the full PDF URL, and question-to-page crop mappings.

    The total score comes from the scores export (``csv_total``): the page
    HTML holds many unrelated ``"score"`` keys (questions, rubric items).
    """
    lines = [f"## Submission Content: {student_name} ({student_email})"]
    lines.append(f"**Submission ID:** `{sub_id}`")
    if csv_total:
        lines.append(f"**Total Score:** {csv_total} (from the scores export)")
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

    return "\n".join(lines)


def get_student_submission_content(course_id: str, assignment_id: str,
                                    student_email: str) -> str:
    """Get the full content of a student's submission, including text answers and image URLs.

    Supports two submission formats:
    1. **Online assignments**: Extracts text answers and uploaded file URLs from
       the AssignmentSubmissionViewer React component.
    2. **Scanned PDF exams**: Extracts per-page scanned images and the full PDF
       from embedded JSON in the raw HTML.

    Requires instructor/TA access. Typed answers are student-authored and are
    returned inside labelled untrusted blocks.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_email: The student's email address (case-insensitive).
    """
    if not course_id or not assignment_id or not student_email or not student_email.strip():
        return "Error: course_id, assignment_id, and student_email are required."

    # First, find the submission ID for the student
    try:
        rows, _fields = _fetch_assignment_scores_csv(course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching scores to find submission: {e}"

    wanted = student_email.strip().casefold()
    row = next((r for r in rows if _row_email(r).casefold() == wanted), None)
    if row is None:
        return f"Error: Could not find student {student_email} in the course roster/scores."

    # Names and emails come from the roster export and may be student-edited:
    # keep them on one line in every heading below.
    student_email = _sanitize_inline(_row_email(row))
    sub_id = (row.get("Submission ID") or "").strip()
    if row.get("Status") == "Missing" or not sub_id:
        return f"Student {student_email} has no submission for this assignment."
    student_name = _sanitize_inline(_row_full_name(row)) or student_email
    csv_total = _csv_total_display(row)

    # Fetch the submission page
    try:
        conn = get_connection()
        sub_url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/submissions/{sub_id}"
        resp2 = conn.session.get(sub_url)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching submission page: {e}"

    if resp2.status_code != 200:
        return f"Error accessing submission {sub_id} (status {resp2.status_code})."

    soup = BeautifulSoup(resp2.text, "html.parser")
    viewer = soup.find(attrs={"data-react-class": "AssignmentSubmissionViewer"})

    # ---- Path A: Online Assignment (has AssignmentSubmissionViewer) ----
    if viewer:
        return _extract_online_submission(
            viewer, student_name, student_email, sub_id, csv_total
        )

    # ---- Path B: Scanned PDF Exam (no React component) ----
    # Check if the page contains embedded page image data
    if "production-gradescope-uploads" in resp2.text and '"number":' in resp2.text:
        return _extract_scanned_pdf_content(
            resp2.text, student_name, student_email, sub_id, csv_total
        )

    return (
        f"## Submission Content: {student_name} ({student_email})\n"
        f"**Submission ID:** `{sub_id}`\n\n"
        "Could not extract submission content. The assignment format "
        "may not be supported yet."
    )


def _collect_answer_parts(
    value, file_map: dict[str, str], texts: list[str], files: list[str],
) -> None:
    """Split one answer value into typed text and uploaded-file references.

    Answers can be ``{'0': 'text'}`` or ``{'0': [{'text_file_id': 123}]}``.
    A file reference whose URL cannot be resolved is still reported, so an
    uploaded answer never reads as a blank one.
    """
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            texts.append(value)
    elif isinstance(value, list):
        for item in value:
            _collect_answer_parts(item, file_map, texts, files)
    elif isinstance(value, dict) and "text_file_id" in value:
        fid = str(value["text_file_id"])
        if fid in file_map:
            files.append(f"[Image/File URL: {file_map[fid]}]")
        else:
            files.append(
                f"[Uploaded file ID: {fid} — file URL not available; open the "
                "submission in Gradescope to view it]"
            )
    elif isinstance(value, dict):
        texts.append(json.dumps(value, ensure_ascii=False, default=str))
    else:
        texts.append(str(value))


def _extract_online_submission(viewer, student_name: str,
                                student_email: str, sub_id: str,
                                csv_total: str | None = None) -> str:
    """Extract content from an online assignment using AssignmentSubmissionViewer."""
    props_str = viewer.get("data-react-props", "{}")
    try:
        props = json.loads(props_str)
    except json.JSONDecodeError:
        return "Error: Could not parse submission data."

    # Extract files and answers. IDs are compared as strings; a file entry
    # may carry its URL under file.url or directly under url.
    file_map: dict[str, str] = {}
    for f in props.get("text_files") or []:
        if not isinstance(f, dict) or f.get("id") is None:
            continue
        file_info = f.get("file") if isinstance(f.get("file"), dict) else {}
        furl = file_info.get("url") or f.get("url")
        if furl:
            file_map[str(f["id"])] = normalize_url(str(furl))

    answers = {"questions": {}}
    q_subs = props.get("question_submissions") or []
    for qs in q_subs:
        if not isinstance(qs, dict):
            continue
        qid = str(qs.get("question_id"))
        ans_data = qs.get("answers") or {}
        values = ans_data.values() if isinstance(ans_data, dict) else [ans_data]

        texts: list[str] = []
        files: list[str] = []
        for val in values:
            _collect_answer_parts(val, file_map, texts, files)

        answers["questions"][qid] = {
            "texts": texts,
            "files": files,
            # Also get current score if graded
            "score": qs.get("score"),
        }

    # Format output
    lines = [f"## Submission Content: {student_name} ({student_email})"]
    lines.append(f"**Submission ID:** `{sub_id}`")

    meta = props.get("assignment_submission") or {}
    if meta.get("score") is not None:
        lines.append(f"**Total Score:** {meta.get('score')} points")
    elif csv_total:
        lines.append(f"**Total Score:** {csv_total} (from the scores export)")
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
        if data["texts"]:
            lines.append(format_untrusted("\n".join(data["texts"]), "STUDENT ANSWER"))
        lines.extend(data["files"])
        if not data["texts"] and not data["files"]:
            lines.append("(No answer provided)")
        lines.append("")

    return "\n".join(lines)
