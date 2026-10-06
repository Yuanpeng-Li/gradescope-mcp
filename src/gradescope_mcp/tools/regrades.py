"""Regrade request management tools.

These tools allow instructors/TAs to view, review, and manage regrade requests
submitted by students. Data is parsed from the Gradescope regrade request pages
and the SubmissionGrader React component.

Regrade messages are student-authored and are returned inside labelled
untrusted blocks.
"""

import copy
import re

from bs4 import BeautifulSoup

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.common import (
    element_classes,
    escape_md_cell,
    format_untrusted,
    is_hidden_element,
    is_placeholder_page,
    normalize_url,
    page_number,
    select_crop_pages,
)
from gradescope_mcp.tools.grading_ops import (
    _applied_rubric_ids,
    _get_grading_context,
    _resolve_rubric,
)


# Completion-cell values that positively mean "not completed yet". Truly
# empty cells (no text and no elements) and placeholder cells also count as
# pending.
_REGRADE_PENDING_TOKENS = frozenset({
    "", "-", "—", "–", "pending", "n/a", "na", "none", "tbd", "no", "n",
    "false", "open", "not completed", "incomplete", "in progress",
    "unresolved", "awaiting response", "✗", "✘", "×",
})

# Completion-cell values that positively mean "completed".
_REGRADE_DONE_TOKENS = frozenset({
    "yes", "y", "true", "completed", "complete", "done", "closed", "resolved",
    "✓", "✔", "✔️", "✅",
})

# Icon-library classes that positively mean "completed" (Font Awesome,
# Bootstrap Icons, Glyphicons, ``icon-*``). Generic names such as ``check``
# or ``checkmark`` are not included: they are also the usual classes of a
# CSS checkbox, which is drawn for both states. Any other icon is
# unreadable, not "pending".
_CHECK_ICON_NAMES = frozenset({
    "icon-check", "icon-checkmark", "icon-check-circle", "fa-check",
    "fa-check-circle", "fa-check-circle-o", "fa-check-square",
    "fa-check-square-o", "fa-circle-check", "fa-square-check", "bi-check",
    "bi-check-lg", "bi-check-circle", "bi-check-circle-fill", "bi-check2",
    "bi-check2-circle", "glyphicon-ok", "glyphicon-check",
})

# SVG sprite symbols (``<use href="...#name">``) that draw a check mark. A
# sprite reference always draws its glyph, so generic symbol names count.
_CHECK_SPRITE_NAMES = _CHECK_ICON_NAMES | {
    "check", "checkmark", "check-mark", "check-circle",
}

# Classes that show an element to screen readers only: their text still
# counts, but an icon carrying one of them is not visible evidence.
_SCREEN_READER_ONLY_CLASSES = frozenset({
    "sr-only", "visually-hidden", "screen-reader-only", "screen-reader-text",
})

# Class-name parts marking an icon as greyed out / not active. A check icon
# styled this way is not read as completed.
_INACTIVE_CLASS_PARTS = frozenset({
    "muted", "disabled", "inactive", "incomplete", "unchecked", "pending",
})

# Elements that carry no completion information of their own.
_LAYOUT_ONLY_TAGS = frozenset({"br", "wbr"})

_PENDING_WORDS_RE = re.compile(
    r"\b(?:not|pending|open|awaiting|incomplete|unresolved|in progress)\b"
)

# A date or time stamp is the usual positive evidence of completion.
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    rf"|\b{_MONTHS}\s+\d{{1,2}}\b"
    rf"|\b\d{{1,2}}\s+{_MONTHS}"
    r"|\b\d{1,2}:\d{2}\b"
    r"|\bago\b"
)

# Header candidates per column, matched on normalized header text: exact
# matches first, then substrings. Columns are resolved in this order so a
# header can only be claimed once ("Question Name" is the question column,
# not the student column).
_COLUMN_CANDIDATES = (
    ("grader", ("grader", "graded by", "assigned grader", "assigned to")),
    ("question", ("question",)),
    ("completed", ("completed", "complete", "decided", "resolved", "closed", "status")),
    ("student", ("student", "user", "name")),
)


def _normalize_header(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def _resolve_columns(headers: list[str]) -> dict[str, int | None]:
    normalized = [_normalize_header(h) for h in headers]
    used: set[int] = set()
    columns: dict[str, int | None] = {}
    for role, candidates in _COLUMN_CANDIDATES:
        found = None
        for match_exact in (True, False):
            for cand in candidates:
                for idx, h in enumerate(normalized):
                    if idx in used or not h:
                        continue
                    if (h == cand) if match_exact else (cand in h):
                        found = idx
                        break
                if found is not None:
                    break
            if found is not None:
                break
        columns[role] = found
        if found is not None:
            used.add(found)
    return columns


def _visible_copy(cell):
    """A copy of ``cell`` without the elements that are not rendered."""
    visible = copy.copy(cell)
    for el in visible.find_all(True):
        if not el.decomposed and is_hidden_element(el):
            el.decompose()
    return visible


def _has_content(cell) -> bool:
    """Whether ``cell`` holds any text or non-layout element."""
    return bool(cell.get_text(strip=True)) or any(
        el.name not in _LAYOUT_ONLY_TAGS for el in cell.find_all(True)
    )


def _checkbox_state(cell) -> tuple[bool, bool | None]:
    """``(found, completed)`` from checkbox controls in ``cell``.

    A checkbox is read by its checked state only, never by its classes:
    all checked = completed, none checked = pending, mixed = unknown.
    """
    states = []
    for el in cell.find_all(True):
        if el.name == "input" and str(el.get("type") or "").lower() == "checkbox":
            states.append(el.has_attr("checked"))
        elif str(el.get("role") or "").lower() == "checkbox":
            value = str(el.get("aria-checked") or "").strip().lower()
            states.append(True if value == "true" else False if value == "false" else None)
    if not states:
        return False, None
    if all(state is True for state in states):
        return True, True
    if all(state is False for state in states):
        return True, False
    return True, None


def _has_check_icon(cell) -> bool:
    """Whether ``cell`` holds a visible check-mark icon.

    ``cell`` must already be stripped of hidden elements. The icon must use
    a specific icon-library class or check sprite, and neither it nor an
    enclosing element in the cell may be screen-reader-only or styled as
    inactive (muted, disabled, unchecked, ...).
    """
    for el in cell.find_all(True):
        is_check = any(name in _CHECK_ICON_NAMES for name in element_classes(el))
        if el.name == "use":
            for attr in ("href", "xlink:href"):
                ref = el.get(attr)
                if isinstance(ref, str) and "#" in ref:
                    sprite = ref.rsplit("#", 1)[1].lower()
                    is_check = is_check or sprite in _CHECK_SPRITE_NAMES
        if not is_check:
            continue
        chain = [el]
        for parent in el.parents:
            if parent is cell:
                break
            chain.append(parent)
        if any(_is_dimmed(node) for node in chain):
            continue
        return True
    return False


def _is_dimmed(el) -> bool:
    """Whether ``el`` is screen-reader-only or styled as inactive."""
    for name in element_classes(el):
        if name in _SCREEN_READER_ONLY_CLASSES:
            return True
        if _INACTIVE_CLASS_PARTS.intersection(re.split(r"[-_]+", name)):
            return True
    return False


def _text_evidence(candidates: list[str]) -> bool | None:
    """Completion stated by a cell's visible text or labels (lower-cased).

    A recognised token first, then a pending word, then a date/time stamp;
    None when none of them says anything.
    """
    for c in candidates:
        if c in _REGRADE_DONE_TOKENS:
            return True
        if c in _REGRADE_PENDING_TOKENS:
            return False
    for c in candidates:
        if _PENDING_WORDS_RE.search(c):
            return False
    for c in candidates:
        if _DATE_RE.search(c):
            return True
    return None


def _classify_completion(cell) -> bool | None:
    """True = completed, False = pending, None = cannot tell.

    Only positive, visible evidence counts as completed: a checked
    checkbox, a date/time stamp, a recognised token, an icon's
    aria-label/title/alt, or a visible icon-library check-mark class.
    Hidden elements (``hidden``, ``d-none``, ``display:none``, ...) are
    ignored; a cell whose only content is hidden is unknown. A checkbox is
    read by its checked state (unchecked = pending), unless the cell's
    visible text or labels say the opposite (a checked box next to
    "Pending", an unchecked one next to "Completed"): conflicting evidence
    is unknown. Only a truly empty cell (no text, no labels, no elements)
    counts as pending; a cell holding an unlabelled icon or image is
    unknown, since Gradescope may render completion as a bare icon. A cell
    whose only content is a link is never read as the completion value.
    """
    if cell is None:
        return None
    visible = _visible_copy(cell)
    found, checked = _checkbox_state(visible)
    cell_has_hidden_content = _has_content(cell) and not _has_content(visible)
    cell = visible
    text = " ".join(cell.get_text(" ", strip=True).split())
    anchors = cell.find_all("a")
    if anchors and text == " ".join(
        " ".join(a.get_text(" ", strip=True).split()) for a in anchors
    ).strip():
        text = ""
        link_only = True
    else:
        link_only = False

    labels = []
    for el in [cell, *cell.find_all(True)]:
        for attr in ("aria-label", "title", "alt", "data-original-title"):
            value = el.get(attr)
            if isinstance(value, str) and value.strip():
                labels.append(" ".join(value.split()))

    candidates = [c.lower() for c in [text, *labels] if c]
    evidence = _text_evidence(candidates)
    if found:
        if evidence is not None and evidence is not checked:
            return None
        return checked
    if evidence is not None:
        return evidence
    if candidates or link_only:
        return None
    if _has_check_icon(cell):
        return True
    if cell_has_hidden_content or any(
        el.name not in _LAYOUT_ONLY_TAGS for el in cell.find_all(True)
    ):
        # An unlabelled icon, image or wrapper, or content that is only
        # hidden: there is something to read, but not as visible text, so
        # the status is unknown rather than pending.
        return None
    # A truly empty completion cell means the request has not been decided.
    return False


def _looks_like_regrade_page(soup: BeautifulSoup) -> bool:
    """Whether the visible page text mentions regrades at all."""
    for tag in soup(["script", "style"]):
        tag.decompose()
    return "regrade" in soup.get_text(" ").lower()


def get_regrade_requests(course_id: str, assignment_id: str) -> str:
    """List all regrade requests for an assignment.

    Returns a table of pending and completed regrade requests with student name,
    question, grader, and status. Requires instructor/TA access.

    A request is marked completed only on positive, visible evidence (a
    checked checkbox, a date/time, a recognised status word, icon label or
    visible icon-library check-mark icon) and pending only on a pending
    word, an unchecked checkbox or a truly empty cell; anything else,
    including an unlabelled icon, a hidden or greyed-out check icon, a
    generic ``check`` class and a checkbox whose state contradicts the
    cell's visible text (a checked box next to "Pending"), is shown as
    unknown (❓) rather than guessed.

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
        if _looks_like_regrade_page(soup):
            return f"No regrade requests found for assignment `{assignment_id}`. (Regrade requests may not be enabled.)"
        return (
            f"Error: unexpected page for assignment `{assignment_id}` regrade "
            "requests: no regrade table and no regrade-related content. The "
            "session may have expired or the page layout changed."
        )

    # Header-aware column resolution. Gradescope drops the Sections column for
    # courses without sections, so positions cannot be assumed.
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
                headers = [th.get_text(strip=True) for th in first_row.find_all(["th", "td"])]

    columns = _resolve_columns(headers)
    unresolved = [
        role for role in ("student", "question", "completed") if columns[role] is None
    ]

    body = table.find("tbody") or table
    data_rows = [tr for tr in body.find_all("tr") if tr.find("td")]
    if not data_rows:
        return f"No regrade requests for assignment `{assignment_id}`."

    lines = [f"## Regrade Requests — Assignment {assignment_id}\n"]
    if unresolved:
        lines.append(
            f"⚠️ **Unrecognized regrade table layout** (headers: {headers or '(none)'}): "
            f"could not find the {', '.join(unresolved)} column(s). Those cells "
            "show '?' and unresolved completion status shows ❓. Check the "
            "regrade page in Gradescope before relying on this list.\n"
        )
    lines.append("| # | Status | Student | Question | Grader | Review Link |")
    lines.append("|---|--------|---------|----------|--------|-------------|")

    pending_count = 0
    completed_count = 0
    unknown_count = 0

    for i, row in enumerate(data_rows, 1):
        # Index th and td together, like the header row, so a row-header <th>
        # cannot shift every column.
        cells = row.find_all(["td", "th"])

        def _cell(role: str):
            idx = columns[role]
            return cells[idx] if idx is not None and idx < len(cells) else None

        def _text(role: str) -> str:
            cell = _cell(role)
            if cell is None:
                return "?"
            return escape_md_cell(" ".join(cell.get_text(" ", strip=True).split()))

        student = _text("student")
        question = _text("question")
        grader = _text("grader") if columns["grader"] is not None else ""
        is_completed = (
            _classify_completion(_cell("completed"))
            if columns["completed"] is not None else None
        )

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

        if is_completed is True:
            status = "✅"
            completed_count += 1
        elif is_completed is False:
            status = "⏳"
            pending_count += 1
        else:
            status = "❓"
            unknown_count += 1

        # Surface the source cell when the IDs cannot be parsed so the user
        # can investigate instead of getting a silently empty Review Link.
        if qid:
            id_info = f"qid={qid}, sid={sid}"
        elif review_link:
            id_info = f"⚠ link parse failed: {escape_md_cell(review_link)}"
        else:
            id_info = "⚠ no /grade link in row"

        lines.append(
            f"| {i} | {status} | {student} | {question} | {grader} | {id_info} |"
        )

    lines.append("")
    summary = f"**Pending:** {pending_count} | **Completed:** {completed_count}"
    if unknown_count:
        summary += f" | **Unknown:** {unknown_count}"
    summary += f" | **Total:** {pending_count + completed_count + unknown_count}"
    lines.append(summary)
    lines.append("")
    lines.append(
        "_Use `get_regrade_detail(course_id, question_id, submission_id)` to see "
        "the student's message and rubric for a specific request._"
    )

    return "\n".join(lines)


def _quote(text) -> str:
    """Blockquote every line (not just the first) of staff-authored text."""
    return "\n".join(f"> {line}" for line in str(text).splitlines() or [""])


def _append_request(lines: list[str], req: dict) -> None:
    lines.append(f"**Date:** {req.get('created_at', 'N/A')}")
    lines.append("\n**Student says:**")
    lines.append(
        format_untrusted(
            req.get("student_comment") or "(no message)", "REGRADE REQUEST MESSAGE"
        )
    )
    lines.append("")
    if req.get("staff_comment"):
        lines.append(f"**Staff response:**\n{_quote(req.get('staff_comment'))}\n")


def get_regrade_detail(course_id: str, question_id: str, submission_id: str) -> str:
    """Get detailed information about a specific regrade request.

    Shows the current question score, point adjustment, grader comment and
    scoring direction, the rubric with applied items, the student's regrade
    message (as an untrusted block), the staff response (if any), and links
    to the relevant submission pages. Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID (from regrade request listing).
        submission_id: The submission ID (from regrade request listing).
    """
    if not course_id or not question_id or not submission_id:
        return "Error: course_id, question_id, and submission_id are required."

    try:
        ctx = _get_grading_context(course_id, question_id, submission_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching regrade detail: {e}"

    props = ctx["props"]
    question = props.get("question") or {}
    submission = props.get("submission") or {}
    evaluation = props.get("evaluation") or {}

    # Extract question info
    q_title = question.get("title", "Unknown")
    q_weight = question.get("weight", "?")

    lines = [f"## Regrade Detail — Question {q_title} (max {q_weight} pts)\n"]

    # Assignment info
    assignment = props.get("assignment") or {}
    if assignment.get("title"):
        lines.append(f"**Assignment:** {assignment.get('title')}")
    lines.append(
        f"**Question ID:** `{question_id}` | **Question Submission ID:** `{submission_id}`"
    )

    # Current grade on this question
    score = submission.get("score")
    lines.append(
        f"**Current question score:** {score if score is not None else 'Ungraded'} / {q_weight}"
    )
    scoring_type = question.get("scoring_type")
    bounds = f"(floor={question.get('floor')}, ceiling={question.get('ceiling')})"
    if scoring_type == "positive":
        lines.append(f"**Scoring:** positive {bounds}")
        lines.append("  ↳ _Rubric items **add** points (e.g. `5.0` = +5 earned)._")
    elif scoring_type == "negative":
        lines.append(f"**Scoring:** negative {bounds}")
        lines.append(
            "  ↳ _Starts at full marks; rubric items **deduct** points "
            "(e.g. `2.0` = −2)._"
        )
    else:
        reported = (
            "not reported by Gradescope" if scoring_type in (None, "")
            else f"unrecognized value `{escape_md_cell(scoring_type)}`"
        )
        lines.append(f"**Scoring:** unknown ({reported}) {bounds}")
        lines.append(
            "  ↳ _Whether rubric items add or deduct points is unknown; confirm "
            "the question's scoring in Gradescope before judging the regrade._"
        )
    points = evaluation.get("points")
    lines.append(f"**Point adjustment:** {points if points is not None else 'None'}")
    if evaluation.get("comments"):
        lines.append(f"**Grader comment:**\n{_quote(evaluation.get('comments'))}")

    lines.append("")

    # Current rubric, resolved the same way as the grading context.
    rubric_items = _resolve_rubric(props)
    applied_ids = _applied_rubric_ids(props)

    if rubric_items:
        lines.append("### Rubric Items")
        lines.append("| Applied | ID | Description | Points |")
        lines.append("|---------|----|-------------|--------|")
        for ri in rubric_items:
            applied = "✅" if str(ri.get("id")) in applied_ids else "—"
            lines.append(
                f"| {applied} | `{escape_md_cell(ri.get('id'))}` "
                f"| {escape_md_cell(ri.get('description', ''))} "
                f"| {escape_md_cell(ri.get('weight', '?'))} |"
            )
        lines.append("")

    # Open regrade request
    open_req = props.get("open_request")
    if isinstance(open_req, dict) and open_req:
        lines.append("### ⏳ Open Regrade Request")
        _append_request(lines, open_req)

    # Closed regrade requests
    closed = [r for r in (props.get("closed_requests") or []) if isinstance(r, dict)]
    if closed:
        lines.append("### Closed Regrade Request(s)")
        for req in closed:
            _append_request(lines, req)

    if not open_req and not closed:
        lines.append("_No regrade requests found for this submission._")

    # Scanned pages: the same selection as the grading context — the crop
    # pages and their neighbours, or every page when there is no crop info or
    # the crop matches none of the submission's pages (mis-tagged pages).
    pages = [
        p for p in (props.get("pages") or [])
        if isinstance(p, dict) and isinstance(p.get("url"), str)
        and not is_placeholder_page(p)
    ]
    if pages:
        crop = (question.get("parameters") or {}).get("crop_rect_list") or []
        crop_pages = sorted({
            n for n in (
                page_number(c.get("page_number")) for c in crop if isinstance(c, dict)
            )
            if n is not None
        })
        shown = select_crop_pages(pages, crop_pages)
        page_numbers = {page_number(p.get("number")) for p in pages}
        lines.append(f"\n### Submission Pages ({len(pages)})")
        if crop_pages:
            lines.append(
                "**Relevant pages (answer region):** "
                + ", ".join(str(n) for n in crop_pages)
            )
            if not page_numbers.intersection(crop_pages):
                lines.append(
                    "⚠️ The answer region's page(s) are not among this submission's "
                    "pages (the student may have tagged other pages); every page "
                    "is listed."
                )
        for p in shown:
            label = escape_md_cell(p.get("number")) if p.get("number") is not None else "?"
            lines.append(f"- Page {label}: [View]({normalize_url(p['url'])})")
        if len(pages) > len(shown):
            lines.append(
                f"- _...and {len(pages) - len(shown)} more pages "
                "(`tool_smart_read_submission` lists every page)_"
            )

    return "\n".join(lines)
