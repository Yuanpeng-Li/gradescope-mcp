"""Grading write operation tools.

These tools enable AI agents to actually grade submissions on Gradescope:
apply rubric items, set point adjustments, add comments, create rubric items,
and navigate between submissions.

All write operations require CSRF tokens extracted from the grading page.
"""

import json
import logging
import re
from typing import Iterable

from bs4 import BeautifulSoup

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.grading import _build_question_tree, _get_outline_data
from gradescope_mcp.tools.safety import write_confirmation_required

logger = logging.getLogger(__name__)

_MISSING_PDF_MARKER = "missing_pdf"


def _normalize_url(url: str) -> str:
    """Normalize protocol-relative URLs to https."""
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _is_placeholder_page(page: dict) -> bool:
    """Check if a page is a placeholder/missing PDF image."""
    url = page.get("url", "")
    return _MISSING_PDF_MARKER in url or not url


def _compute_new_score(
    props: dict,
    apply_ids: Iterable[str] | None,
    point_adjustment: float | int | None,
) -> tuple[float | None, set[str]]:
    """Compute the resulting score from rubric items + adjustment.

    Mirrors Gradescope's scoring semantics so callers can report the real
    post-write score without an extra round-trip. The save endpoint does not
    consistently echo the new score back, so deriving it locally is more
    reliable than parsing the response.

    Returns a ``(score, unknown_ids)`` tuple. ``score`` is ``None`` if the
    question weight is unknown. ``unknown_ids`` contains any IDs in
    ``apply_ids`` that don't exist in the question's current rubric — those
    contribute 0 to the local sum but Gradescope may reject them server-side,
    so callers should surface them to the user.
    """
    question = props.get("question", {}) or {}
    weight = question.get("weight")
    if weight is None:
        return None, set()
    try:
        weight_f = float(weight)
    except (TypeError, ValueError):
        return None, set()

    scoring_type = question.get("scoring_type", "negative")
    floor = question.get("floor")
    ceiling = question.get("ceiling")
    # Gradescope defaults floor and ceiling to True when unspecified.
    floor = True if floor is None else bool(floor)
    ceiling = True if ceiling is None else bool(ceiling)

    rubric_items = props.get("rubric_items", []) or []
    weight_by_id: dict[str, float] = {}
    for ri in rubric_items:
        try:
            weight_by_id[str(ri["id"])] = float(ri.get("weight") or 0)
        except (TypeError, ValueError):
            weight_by_id[str(ri["id"])] = 0.0

    apply_set = {str(rid) for rid in (apply_ids or [])}
    unknown_ids = apply_set - weight_by_id.keys()
    applied_sum = sum(weight_by_id.get(rid, 0.0) for rid in apply_set)

    try:
        pa = float(point_adjustment) if point_adjustment is not None else 0.0
    except (TypeError, ValueError):
        pa = 0.0

    if scoring_type == "positive":
        score = applied_sum + pa
    else:
        score = weight_f - applied_sum + pa

    if floor and score < 0:
        score = 0.0
    if ceiling and score > weight_f:
        score = weight_f
    return score, unknown_ids


def _format_score(score: float | None) -> str:
    """Render a score for display: drop ``.0`` for integer-valued floats."""
    if score is None:
        return "?"
    if float(score).is_integer():
        return str(int(score))
    return f"{score:g}"


def _safe_weight(weight: object) -> float:
    try:
        return float(weight or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_full_credit_items(props: dict) -> tuple[list[str] | None, str | None]:
    """Resolve the rubric item(s) representing full credit for a question.

    In negative scoring, "full credit" means checking the 0-point benchmark
    item (the one usually labelled "Correct"). Checking *nothing* leaves the
    submission **ungraded** even though the displayed score is full, so callers
    that want full credit must apply that item.

    Returns ``(item_ids, None)`` when exactly one zero-weight item exists,
    otherwise ``(None, error_message)`` so the caller can ask for an explicit
    ``rubric_item_ids`` instead of guessing.
    """
    question = props.get("question", {}) or {}
    scoring_type = question.get("scoring_type", "negative")
    if scoring_type != "negative":
        return None, (
            "full_credit is only supported for negative-scoring questions "
            f"(this question is '{scoring_type}'); pass rubric_item_ids explicitly."
        )
    zero_items = [
        str(ri["id"])
        for ri in (props.get("rubric_items", []) or [])
        if "id" in ri and _safe_weight(ri.get("weight")) == 0.0
    ]
    if len(zero_items) == 1:
        return zero_items, None
    if not zero_items:
        return None, (
            "full_credit could not find a 0-point benchmark item to mark this "
            "submission graded; pass the benchmark item's ID in rubric_item_ids."
        )
    return None, (
        f"full_credit found multiple 0-point items ({zero_items}); cannot pick "
        "one automatically. Pass the intended item's ID in rubric_item_ids."
    )


def _get_grading_context(course_id: str, question_id: str, submission_id: str) -> dict:
    """Fetch the SubmissionGrader page and extract all context needed for grading.

    Returns a dict with:
        - props: the SubmissionGrader React component props
        - csrf_token: the CSRF token for POST requests
        - session: the authenticated session
        - base_url: Gradescope base URL
    """
    conn = get_connection()
    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/questions/{question_id}/submissions/{submission_id}/grade"
    )
    resp = conn.session.get(url)

    if resp.status_code != 200:
        hint = ""
        if resp.status_code == 404:
            hint = (
                " This often means you are using a Global Submission ID "
                "(from get_assignment_submissions) instead of a Question "
                "Submission ID. Use get_next_ungraded or get_grading_progress "
                "to obtain the correct Question Submission ID."
            )
        raise ValueError(
            f"Cannot access grading page (status {resp.status_code}).{hint}"
        )

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract CSRF token
    csrf_meta = soup.find("meta", {"name": "csrf-token"})
    csrf_token = csrf_meta.get("content", "") if csrf_meta else ""

    # Extract SubmissionGrader props
    grader = soup.find(attrs={"data-react-class": "SubmissionGrader"})
    if not grader:
        raise ValueError("SubmissionGrader component not found.")

    props = json.loads(grader.get("data-react-props", "{}"))

    return {
        "props": props,
        "csrf_token": csrf_token,
        "session": conn.session,
        "base_url": conn.gradescope_base_url,
    }


def get_submission_grading_context(
    course_id: str,
    question_id: str,
    submission_id: str,
    output_format: str = "markdown",
) -> str:
    """Get the full grading context for a specific question submission.

    Returns the current rubric items, applied evaluations, score, comments,
    navigation URLs, and student info. This is what you need before grading.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        submission_id: The question submission ID (NOT the assignment submission ID).
        output_format: "markdown" (default) or "json" for structured output.
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
        return f"Error fetching grading context: {e}"

    props = ctx["props"]
    question = props.get("question", {})
    submission = props.get("submission", {})
    evaluation = props.get("evaluation", {})
    nav = props.get("navigation_urls", {})

    # Rubric items + evaluations
    rubric_items = props.get("rubric_items", [])
    evaluations = props.get("rubric_item_evaluations", [])
    applied_ids = {e["rubric_item_id"] for e in evaluations if e.get("present")}

    # Navigation parsing — filter out self-referencing ungraded links
    # (Gradescope sets next_ungraded/previous_ungraded to the current
    # submission when it is itself ungraded, which misleads agents)
    nav_parsed = {}
    for label, key in [
        ("previous_ungraded", "previous_ungraded"),
        ("next_ungraded", "next_ungraded"),
        ("previous_submission", "previous_submission"),
        ("next_submission", "next_submission"),
        ("previous_question", "previous_question"),
        ("next_question", "next_question"),
    ]:
        url = nav.get(key, "")
        if url:
            qid_m = re.search(r"/questions/(\d+)", url)
            sid_m = re.search(r"/submissions/(\d+)", url)
            if qid_m and sid_m:
                parsed_sid = sid_m.group(1)
                # Skip self-referencing ungraded links
                if key in ("previous_ungraded", "next_ungraded") and parsed_sid == submission_id:
                    continue
                nav_parsed[label] = {
                    "question_id": qid_m.group(1),
                    "submission_id": parsed_sid,
                }

    # Answer group info
    answer_group_id = props.get("answer_group")
    answer_group_size = props.get("answer_group_size")
    groups_present = props.get("groups_present", False)

    # Extract text answer (for online assignments)
    answers_data = submission.get("answers", {})
    text_content = []
    if isinstance(answers_data, dict):
        for key, val in answers_data.items():
            if isinstance(val, str):
                text_content.append(val)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        text_content.append(item)
                    elif isinstance(item, dict) and "text_file_id" in item:
                        text_content.append(f"[Uploaded file ID: {item['text_file_id']}]")
                    else:
                        text_content.append(str(item))
    text_answer = "\n".join(text_content).strip() if text_content else None

    # Pages
    pages = props.get("pages", [])
    parameters = question.get("parameters") or {}
    crop = parameters.get("crop_rect_list", [])

    if output_format == "json":
        result = {
            "question_id": question_id,
            "submission_id": submission_id,
            "question_title": question.get("title", ""),
            "weight": question.get("weight"),
            "scoring_type": question.get("scoring_type", "negative"),
            "student": submission.get("owner_names", "Unknown"),
            "score": submission.get("score"),
            "graded": submission.get("graded", False),
            "point_adjustment": evaluation.get("points"),
            "comments": evaluation.get("comments", ""),
            "text_answer": text_answer,
            "rubric_items": [
                {
                    "id": str(ri["id"]),
                    "description": ri.get("description", ""),
                    "weight": ri.get("weight"),
                    "applied": ri["id"] in applied_ids,
                    "position": ri.get("position"),
                    "locked": ri.get("locked", False),
                }
                for ri in rubric_items
            ],
            "navigation": nav_parsed,
            "progress": {
                "graded": props.get("num_graded_submissions", 0),
                "total": props.get("num_submissions", 0),
            },
            "answer_group": {
                "id": str(answer_group_id) if answer_group_id else None,
                "size": answer_group_size,
                "groups_present": groups_present,
            } if groups_present else None,

            "pages": [
                {"number": p.get("number"), "url": _normalize_url(p.get("url", ""))}
                for p in pages if isinstance(p, dict) and p.get("url")
                and not _is_placeholder_page(p)
            ][:5],
            "crop_regions": crop,
        }
        return json.dumps(result, indent=2)

    # Markdown output
    lines = [f"## Grading Context — Q{question.get('title', '?')}"]
    lines.append(f"**Question ID:** `{question_id}` | **Question Submission ID:** `{submission_id}`")
    lines.append(f"**Student:** {submission.get('owner_names', 'Unknown')}")
    lines.append(f"**Weight:** {question.get('weight', '?')} pts")
    lines.append(f"**Current Score:** {submission.get('score', 'Ungraded')}")
    lines.append(f"**Graded:** {submission.get('graded', False)}")

    # Scoring type
    scoring_type = question.get("scoring_type", "negative")
    lines.append(f"**Scoring:** {scoring_type} (floor={question.get('floor')}, ceiling={question.get('ceiling')})")
    if scoring_type == "positive":
        lines.append("  ↳ _Rubric items **add** points. Weight values are positive (e.g., `5.0` = +5 earned)._")
    else:
        lines.append("  ↳ _Starts at full marks. Rubric items **deduct** points. Weight values are positive (e.g., `2.0` = −2 deducted). Gradescope handles the sign internally._")

    # Current evaluation (comments + point adjustment)
    if evaluation:
        points = evaluation.get("points")
        comments = evaluation.get("comments", "")
        lines.append(f"\n**Point Adjustment:** {points if points is not None else 'None'}")
        if comments:
            lines.append(f"**Comments:** {comments}")

    # Answer group
    if groups_present and answer_group_id:
        lines.append(f"\n**Answer Group:** `{answer_group_id}` ({answer_group_size} in group)")

    if text_answer:
        lines.append(f"\n### Student Answer")
        lines.append(f"{text_answer}")

    if rubric_items:
        lines.append(f"\n### Rubric Items ({len(rubric_items)})")
        lines.append("| Applied | ID | Description | Points |")
        lines.append("|---------|-----|-------------|--------|")
        for ri in rubric_items:
            applied = "✅" if ri["id"] in applied_ids else "—"
            lines.append(
                f"| {applied} | `{ri['id']}` | {ri['description']} | {ri['weight']} |"
            )

    # Navigation
    lines.append(f"\n### Navigation")
    for label, ids in nav_parsed.items():
        lines.append(f"- **{label}**: qid=`{ids['question_id']}`, sid=`{ids['submission_id']}`")

    lines.append(f"\n**Progress:** {props.get('num_graded_submissions', 0)}/{props.get('num_submissions', 0)} graded")

    # Scanned PDF pages for this question
    if pages:
        lines.append(f"\n### Submission Pages ({len(pages)})")
        if crop:
            crop_pages = sorted(set(c.get("page_number") for c in crop if "page_number" in c))
            lines.append(f"**Relevant pages:** {crop_pages}")
        for p in pages[:3]:
            if isinstance(p, dict) and p.get("url") and not _is_placeholder_page(p):
                page_num = p.get('number') or '?'
                lines.append(f"- Page {page_num}: [View]({_normalize_url(p['url'])})")
        real_pages = [p for p in pages if isinstance(p, dict) and not _is_placeholder_page(p)]
        if len(real_pages) > 3:
            lines.append(f"- _...and {len(real_pages) - 3} more pages_")

    return "\n".join(lines)


def get_question_rubric(course_id: str, question_id: str) -> str:
    """Get rubric items for a question without requiring a submission ID.

    Useful when you know the question_id from get_assignment_outline but
    don't have a specific submission ID. Auto-discovers a submission to
    extract rubric data.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID from outline.
    """
    if not course_id or not question_id:
        return "Error: both course_id and question_id are required."

    try:
        conn = get_connection()
        ctx = None

        # Path 1: submissions page
        url1 = (
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/questions/{question_id}/submissions"
        )
        resp = conn.session.get(url1)
        if resp.status_code == 200:
            match = re.search(
                rf"/courses/{course_id}/questions/{question_id}/submissions/(\d+)/grade",
                resp.text,
            )
            if match:
                ctx = _get_grading_context(course_id, question_id, match.group(1))

        # Path 2: grade page (may redirect to a specific submission)
        if ctx is None:
            url2 = (
                f"{conn.gradescope_base_url}/courses/{course_id}"
                f"/questions/{question_id}/grade"
            )
            resp2 = conn.session.get(url2, allow_redirects=True)
            if resp2.status_code == 200:
                sub_match = re.search(
                    rf"/questions/{question_id}/submissions/(\d+)",
                    resp2.url,
                )
                if sub_match:
                    ctx = _get_grading_context(
                        course_id, question_id, sub_match.group(1)
                    )
                else:
                    # Check if the page itself has SubmissionGrader props
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp2.text, "html.parser")
                    grader = soup.find(attrs={"data-react-class": "SubmissionGrader"})
                    if grader:
                        import json as _json
                        props = _json.loads(grader.get("data-react-props", "{}"))
                        rubric_items = props.get("rubric_items", [])
                        if rubric_items:
                            question = props.get("question", {})
                            ctx = {"props": props}

        if ctx is None:
            return (
                f"No submissions found for question `{question_id}`. "
                "Cannot access rubric. The question may be in a special "
                "assignment type. Try using `get_submission_grading_context` "
                "with a known Question Submission ID instead."
            )
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching rubric: {e}"

    props = ctx["props"]
    question = props.get("question", {})
    # Use props.rubric_items (same source as grading context), fallback to question.rubric
    rubric_items = props.get("rubric_items", []) or question.get("rubric", [])

    if not rubric_items:
        return f"No rubric items found for question `{question_id}`. You can create them with `tool_create_rubric_item`."

    weight = question.get("weight", "?")
    scoring_type = question.get("scoring_type", "negative")

    lines = [f"## Rubric for Question `{question_id}`\n"]
    lines.append(f"**Weight:** {weight} pts")
    lines.append(f"**Scoring:** {scoring_type}\n")
    lines.append("| ID | Description | Points |")
    lines.append("|----|-------------|--------|")

    for item in rubric_items:
        desc = item.get("description", "(no description)")
        # Escape pipes in description
        desc = desc.replace("|", "\\|")
        lines.append(
            f"| `{item['id']}` | {desc} | {item.get('weight', 0)} |"
        )

    return "\n".join(lines)


_SCORE_HEADER_NAMES = ("score", "points", "grade")
_USER_HEADER_NAMES = ("user", "student", "name")
_GRADED_FLAG_HEADER_NAMES = ("graded?", "graded", "status")
_NONEMPTY_SCORE_RE = re.compile(
    r"^\s*"
    r"(?:\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?"  # N/N or N.N/N.N
    r"|\d+(?:\.\d+)?"                         # plain number
    r"|Graded|✓|✅)"
    r"\s*$"
)
_INDEX_LIKE_RE = re.compile(r"^\d{1,4}$")
# Affirmative values that the "Graded?" column is known to use. Anything else
# (em-dash placeholders, "No", "Pending", ...) must NOT be parsed as graded.
_GRADED_AFFIRMATIVE = frozenset({"yes", "y", "true", "graded", "done", "✓", "✅"})


def _resolve_header_index(headers: list[str], wanted: tuple[str, ...]) -> int | None:
    """Return the first column index whose lowercased header matches a wanted name."""
    for idx, name in enumerate(headers):
        norm = (name or "").strip().lower()
        if norm in wanted:
            return idx
    return None


def _strip_email_paren(text: str) -> str:
    """Drop a trailing ``(email@host)`` parenthetical from a user-cell string."""
    return re.sub(r"\s*\([^)]*@[^)]*\)\s*$", "", text).strip()


def _fetch_question_submission_entries(
    course_id: str,
    question_id: str,
) -> list[dict[str, str | bool]]:
    """Return parsed question-submission entries from the submissions page.

    Real Gradescope submission tables include a leading row-index column
    (just bare integers ``1``, ``2``, ...). Earlier heuristic-based parsing
    treated those integers as both ``student_name`` *and* a ``score``
    indicator, so every row appeared graded with a numeric "name". This
    parser instead uses the table headers to identify the User, Score, and
    Graded? columns explicitly, and falls back to a column-index-aware
    heuristic only when headers are missing.
    """
    conn = get_connection()
    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/questions/{question_id}/submissions"
    )
    resp = conn.session.get(url)
    if resp.status_code != 200:
        raise ValueError(
            f"Cannot access submissions page for question "
            f"`{question_id}` (status {resp.status_code})."
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(
        rf"/courses/{re.escape(course_id)}/questions/{re.escape(question_id)}"
        rf"/submissions/(\d+)/grade"
    )

    # Map each table that contains grade links to its header row indices.
    table_columns: dict[int, dict[str, int | None]] = {}

    def columns_for(table) -> dict[str, int | None]:
        cached = table_columns.get(id(table))
        if cached is not None:
            return cached
        headers: list[str] = []
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
        cols = {
            "user": _resolve_header_index(headers, _USER_HEADER_NAMES),
            "score": _resolve_header_index(headers, _SCORE_HEADER_NAMES),
            "graded_flag": _resolve_header_index(headers, _GRADED_FLAG_HEADER_NAMES),
        }
        table_columns[id(table)] = cols
        return cols

    seen: set[str] = set()
    entries: list[dict[str, str | bool]] = []
    for link in soup.find_all("a", href=pattern):
        match = pattern.search(link.get("href", ""))
        if not match:
            continue
        sid = match.group(1)
        if sid in seen:
            continue
        seen.add(sid)

        row = link.find_parent("tr")
        student_name = ""
        graded = False

        if row is not None:
            tds = row.find_all("td")
            cell_texts = [td.get_text(strip=True) for td in tds]

            table = row.find_parent("table")
            cols = columns_for(table) if table is not None else {
                "user": None, "score": None, "graded_flag": None,
            }

            # Student name resolution.
            user_idx = cols["user"]
            if user_idx is not None and user_idx < len(cell_texts):
                student_name = _strip_email_paren(cell_texts[user_idx])
            if not student_name:
                # Fallback: first cell that looks name-like (not a row index,
                # not the submission ID, not a URL).
                for text in cell_texts:
                    if not text or text == sid or text.startswith("/"):
                        continue
                    if _INDEX_LIKE_RE.match(text):
                        continue
                    student_name = _strip_email_paren(text)
                    break

            # Graded resolution: prefer the explicit Graded? column, then the
            # Score column, then the heuristic — but never against the row
            # index column.
            score_idx = cols["score"]
            graded_idx = cols["graded_flag"]

            if graded_idx is not None and graded_idx < len(cell_texts):
                flag = cell_texts[graded_idx].strip().lower()
                if flag in _GRADED_AFFIRMATIVE:
                    graded = True

            if not graded and score_idx is not None and score_idx < len(cell_texts):
                cell = cell_texts[score_idx].strip()
                if cell and cell not in {"-", "—", "–"}:
                    graded = bool(_NONEMPTY_SCORE_RE.match(cell))

            if not graded and score_idx is None and graded_idx is None:
                # Headerless fallback: scan cells from the right but skip the
                # leading column (which Gradescope uses for row indices).
                start = 1 if len(cell_texts) > 1 else 0
                for cell in reversed(cell_texts[start:]):
                    cell = cell.strip()
                    if not cell:
                        continue
                    if _NONEMPTY_SCORE_RE.match(cell):
                        graded = True
                        break

        entries.append(
            {
                "submission_id": sid,
                "student_name": student_name,
                "graded": graded,
            }
        )

    entries.sort(key=lambda entry: int(str(entry["submission_id"])))
    return entries


def _fallback_next_ungraded_submission_id(
    course_id: str,
    question_id: str,
    current_sid: str,
) -> str | None:
    """Fallback path when navigation_urls.next_ungraded is missing or stale."""
    entries = _fetch_question_submission_entries(course_id, question_id)
    ungraded_ids = [
        str(entry["submission_id"])
        for entry in entries
        if not entry.get("graded", False)
    ]
    if not ungraded_ids:
        return None

    if current_sid and current_sid in ungraded_ids:
        current_index = ungraded_ids.index(current_sid)
        if current_index + 1 < len(ungraded_ids):
            return ungraded_ids[current_index + 1]
        return current_sid

    for sid in ungraded_ids:
        if not current_sid or int(sid) > int(current_sid):
            return sid
    return ungraded_ids[0]


def _try_fallback_navigation(
    course_id: str,
    question_id: str,
    current_sid: str,
    props: dict,
    output_format: str,
) -> str:
    """Attempt fallback navigation; always returns a displayable result string."""
    try:
        fallback_sid = _fallback_next_ungraded_submission_id(
            course_id, question_id, current_sid
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"
    if fallback_sid is None:
        return "All submissions for this question are graded! 🎉"
    if fallback_sid == current_sid and not props.get("submission", {}).get("graded", False):
        return (
            "This is the only ungraded submission remaining for this "
            "question.  Grade it first, then call `get_next_ungraded` "
            "again to advance."
        )
    return get_submission_grading_context(
        course_id, question_id, fallback_sid, output_format
    )


def apply_grade(
    course_id: str,
    question_id: str,
    submission_id: str,
    rubric_item_ids: list[str] | None = None,
    point_adjustment: float | None = None,
    comment: str | None = None,
    confidence: float | None = None,
    confirm_write: bool = False,
    full_credit: bool = False,
) -> str:
    """Apply a grade to a student's question submission.

    This is the main grading tool. It can:
    1. Apply/remove rubric items (toggle which are checked)
    2. Set a submission-specific point adjustment
    3. Add a grader comment
    4. Mark full credit (``full_credit=True``)

    **WARNING**: This modifies student grades. Use with caution.

    The ``comment`` parameter maps to Gradescope's "Provide comments
    specific to this submission" field — it is **separate** from
    rubric items in the wire payload, so applying a comment does not
    silently clear rubric state and vice versa.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        submission_id: The question submission ID.
        rubric_item_ids: List of rubric item IDs to apply (checked). Items NOT in
            this list will be unchecked. Pass None to keep current rubric unchanged.
        point_adjustment: Submission-specific point adjustment (can be negative).
            Pass None to keep current adjustment unchanged.
        comment: Per-submission grader comment.
            - ``None`` (default): keep the existing comment unchanged.
            - ``""`` (empty string): **clear** any existing comment.
            - Any other string: overwrite the comment with this text.
            Stored in Gradescope's ``question_submission_evaluation.comments``
            field, independent of ``rubric_item_ids``.
        confidence: Agent's self-assessed grading confidence (0.0-1.0).
            - < 0.6: Grade will be REJECTED — skip or flag for human review.
            - 0.6-0.8: Grade will proceed with a warning to review.
            - > 0.8: Grade proceeds normally.
            - None: No confidence gating (manual grading mode).
        confirm_write: Must be True to save the grade.
        full_credit: When True, marks the submission full credit by checking the
            question's 0-point benchmark ("Correct") item. Use this instead of
            an empty ``rubric_item_ids`` list: clearing all items does NOT mark
            the submission graded in Gradescope. Negative-scoring questions
            only; cannot be combined with ``rubric_item_ids``.
    """
    if not course_id or not question_id or not submission_id:
        return "Error: course_id, question_id, and submission_id are required."

    # Defensive coercion: MCP clients / LLMs sometimes pass a single string
    # instead of a list.  Wrap it so the rest of the logic works.
    if isinstance(rubric_item_ids, str):
        rubric_item_ids = [rubric_item_ids]

    if (
        rubric_item_ids is None
        and point_adjustment is None
        and comment is None
        and not full_credit
    ):
        return (
            "Error: at least one of rubric_item_ids, point_adjustment, comment, "
            "or full_credit must be provided."
        )

    # Confidence gate: reject low-confidence grades
    if confidence is not None:
        if confidence < 0.0 or confidence > 1.0:
            return "Error: confidence must be between 0.0 and 1.0."
        if confidence < 0.6:
            return (
                f"⚠️ **Grade REJECTED** — Your confidence is `{confidence:.2f}` (below 0.6 threshold).\n"
                f"**Action:** Skip this submission or flag for human review.\n"
                f"- submission_id: `{submission_id}`\n"
                f"- Tip: If the handwriting is unclear or the answer is ambiguous, "
                f"move to the next submission with `tool_get_next_ungraded`."
            )

    try:
        ctx = _get_grading_context(course_id, question_id, submission_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching grading context: {e}"

    props = ctx["props"]
    session = ctx["session"]
    csrf_token = ctx["csrf_token"]
    base_url = ctx["base_url"]

    # Resolve full_credit into an explicit rubric selection so the preview and
    # the save payload both reflect it.
    if full_credit:
        if rubric_item_ids is not None:
            return "Error: pass either full_credit=True or rubric_item_ids, not both."
        fc_ids, fc_err = _resolve_full_credit_items(props)
        if fc_err:
            return f"Error: {fc_err}"
        rubric_item_ids = fc_ids

    if not confirm_write:
        details = [
            f"course_id=`{course_id}`",
            f"question_id=`{question_id}`",
            f"submission_id=`{submission_id}`",
            f"current_score={props.get('submission', {}).get('score', 'Ungraded')}",
        ]
        if rubric_item_ids is not None:
            details.append(f"rubric_item_ids={sorted(rubric_item_ids)}")
        if point_adjustment is not None:
            details.append(f"point_adjustment={point_adjustment}")
        if comment is not None:
            details.append(f"comment={comment}")
        return write_confirmation_required("apply_grade", details)

    save_url = props.get("urls", {}).get("save_grade")
    if not save_url:
        return "Error: save_grade URL not found in grading context."

    # Build the payload
    # Gradescope expects a specific format for saving grades
    rubric_items = props.get("rubric_items", [])
    current_evals = props.get("rubric_item_evaluations", [])
    current_eval = props.get("evaluation", {})

    # Build rubric_item_ids_to_apply
    if rubric_item_ids is not None:
        # User specified which rubric items to apply
        apply_ids = set(rubric_item_ids)
    else:
        # Keep current state
        apply_ids = {str(e["rubric_item_id"]) for e in current_evals if e.get("present")}

    # Build the JSON payload matching what the Gradescope frontend sends.
    # Structure: {"rubric_items": {"ID": {"score": "true"/"false"}, ...},
    #             "question_submission_evaluation": {"points": ..., "comments": ...}}
    rubric_items_payload = {}
    for ri in rubric_items:
        rid = str(ri["id"])
        rubric_items_payload[rid] = {
            "score": "true" if rid in apply_ids else "false"
        }

    resolved_points = (
        point_adjustment if point_adjustment is not None
        else current_eval.get("points")
    )
    resolved_comments = (
        comment if comment is not None
        else current_eval.get("comments")
    )

    json_payload = {
        "rubric_items": rubric_items_payload,
        "question_submission_evaluation": {
            "points": resolved_points,
            "comments": resolved_comments,
        },
    }

    # POST to save_grade
    headers = {
        "X-CSRF-Token": csrf_token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        resp = session.post(
            f"{base_url}{save_url}",
            json=json_payload,
            headers=headers,
        )
    except Exception as e:
        return f"Error saving grade: {e}"

    if resp.status_code == 200:
        new_score, unknown_ids = _compute_new_score(props, apply_ids, point_adjustment)
        warning = ""
        if unknown_ids:
            warning = (
                f"\n⚠️ **Unknown rubric IDs (ignored locally, may be rejected by "
                f"Gradescope):** {sorted(unknown_ids)}"
            )
        return (
            f"✅ Grade saved successfully!\n"
            f"**New score:** {_format_score(new_score)}/"
            f"{props.get('question', {}).get('weight', '?')}\n"
            f"**Rubric items applied:** {list(apply_ids)}\n"
            f"**Point adjustment:** {point_adjustment}\n"
            f"**Comment:** {comment or '(unchanged)'}"
            f"{warning}"
        )
    else:
        return f"Error: Grade save failed (status {resp.status_code}). Response: {resp.text[:300]}"


def apply_grade_batch(
    course_id: str,
    question_id: str,
    grades: list[dict],
    confirm_write: bool = False,
) -> str:
    """Apply grades to many submissions for one question in a single call.

    Each ``grades`` entry is a dict:
        - ``submission_id``: str (required)
        - ``rubric_item_ids``: list[str] | None — same semantics as
          ``apply_grade``: ``None`` keeps current rubric state, ``[]`` clears
          all items, a list overwrites with that exact set.
        - ``point_adjustment``: float | None — ``None`` keeps current.
        - ``comment``: str | None — per-submission comment (Gradescope's
          "Provide comments specific to this submission" field, separate
          from rubric items). ``None`` keeps the existing comment, ``""``
          clears it, any other string overwrites.
        - ``confidence``: float | None — per-row gate (<0.6 is rejected).
        - ``full_credit``: bool — when true, marks the row full credit via the
          question's 0-point benchmark item (negative scoring only). Use this
          instead of ``rubric_item_ids: []``, which clears items but leaves the
          submission ungraded. Cannot be combined with ``rubric_item_ids``.

    All entries are applied to the same ``question_id``. Confidence gating is
    per-row. On ``confirm_write=False`` returns a compact preview table.
    On ``confirm_write=True`` returns an execution summary (succeeded /
    failed / skipped-by-confidence) with per-row details.

    This is meant for the main agent's post-approval execution phase. Subagents
    cannot call write-gated tools in the Claude Code harness, so all writes
    must funnel through the main agent; a batch variant cuts round-trips
    dramatically for large grading runs.
    """
    if not course_id or not question_id:
        return "Error: course_id and question_id are required."
    if not isinstance(grades, list) or not grades:
        return "Error: grades must be a non-empty list."

    normalized: list[dict] = []
    errors: list[str] = []
    for i, g in enumerate(grades):
        if not isinstance(g, dict):
            errors.append(f"row {i}: entry must be an object")
            continue
        sid = g.get("submission_id")
        if not sid:
            errors.append(f"row {i}: submission_id is required")
            continue
        rids = g.get("rubric_item_ids")
        if isinstance(rids, str):
            rids = [rids]
        pa = g.get("point_adjustment")
        cm = g.get("comment")
        cf = g.get("confidence")
        fc = bool(g.get("full_credit"))
        if cf is not None and not (0.0 <= cf <= 1.0):
            errors.append(f"row {i} ({sid}): confidence must be between 0.0 and 1.0")
            continue
        if rids is None and pa is None and cm is None and not fc:
            errors.append(
                f"row {i} ({sid}): at least one of rubric_item_ids, "
                "point_adjustment, comment, or full_credit is required"
            )
            continue
        if fc and rids is not None:
            errors.append(
                f"row {i} ({sid}): pass either full_credit or rubric_item_ids, not both"
            )
            continue
        normalized.append(
            {
                "submission_id": str(sid),
                "rubric_item_ids": rids,
                "point_adjustment": pa,
                "comment": cm,
                "confidence": cf,
                "full_credit": fc,
            }
        )
    if errors:
        return "Error: invalid batch input:\n" + "\n".join(f"- {e}" for e in errors)

    if not confirm_write:
        preview_lines = [
            "| # | submission_id | rubric_item_ids | point_adj | comment | confidence |",
            "|---|---------------|-----------------|-----------|---------|------------|",
        ]
        for i, g in enumerate(normalized, 1):
            if g.get("full_credit"):
                rids_disp = "full credit (0-pt item)"
            elif g["rubric_item_ids"] is not None:
                rids_disp = str(sorted(g["rubric_item_ids"]))
            else:
                rids_disp = "(keep current)"
            pa_disp = (
                str(g["point_adjustment"])
                if g["point_adjustment"] is not None
                else "(keep current)"
            )
            cm_disp = g["comment"] if g["comment"] is not None else "(none)"
            cf_disp = (
                f"{g['confidence']:.2f}" if g["confidence"] is not None else "(none)"
            )
            preview_lines.append(
                f"| {i} | `{g['submission_id']}` | {rids_disp} | "
                f"{pa_disp} | {cm_disp} | {cf_disp} |"
            )
        details = [
            f"course_id=`{course_id}`",
            f"question_id=`{question_id}`",
            f"rows={len(normalized)}",
        ]
        return (
            write_confirmation_required("apply_grade_batch", details)
            + "\n\n"
            + "\n".join(preview_lines)
        )

    succeeded: list[tuple[str, object, object, set[str]]] = []
    failed: list[tuple[str, str]] = []
    skipped_confidence: list[tuple[str, float]] = []

    for g in normalized:
        sid = g["submission_id"]
        cf = g["confidence"]
        if cf is not None and cf < 0.6:
            skipped_confidence.append((sid, cf))
            continue

        try:
            ctx = _get_grading_context(course_id, question_id, sid)
        except AuthError as e:
            failed.append((sid, f"Authentication error: {e}"))
            continue
        except ValueError as e:
            failed.append((sid, str(e)))
            continue
        except Exception as e:
            failed.append((sid, f"Error fetching grading context: {e}"))
            continue

        props = ctx["props"]
        session = ctx["session"]
        csrf_token = ctx["csrf_token"]
        base_url = ctx["base_url"]

        save_url = props.get("urls", {}).get("save_grade")
        if not save_url:
            failed.append((sid, "save_grade URL not found in grading context"))
            continue

        if g.get("full_credit"):
            fc_ids, fc_err = _resolve_full_credit_items(props)
            if fc_err:
                failed.append((sid, fc_err))
                continue
            g["rubric_item_ids"] = fc_ids

        rubric_items = props.get("rubric_items", [])
        current_evals = props.get("rubric_item_evaluations", [])
        current_eval = props.get("evaluation", {})

        if g["rubric_item_ids"] is not None:
            apply_ids = set(g["rubric_item_ids"])
        else:
            apply_ids = {
                str(e["rubric_item_id"])
                for e in current_evals
                if e.get("present")
            }

        rubric_items_payload = {}
        for ri in rubric_items:
            rid = str(ri["id"])
            rubric_items_payload[rid] = {
                "score": "true" if rid in apply_ids else "false"
            }

        resolved_points = (
            g["point_adjustment"]
            if g["point_adjustment"] is not None
            else current_eval.get("points")
        )
        resolved_comments = (
            g["comment"]
            if g["comment"] is not None
            else current_eval.get("comments")
        )

        json_payload = {
            "rubric_items": rubric_items_payload,
            "question_submission_evaluation": {
                "points": resolved_points,
                "comments": resolved_comments,
            },
        }

        headers = {
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            resp = session.post(
                f"{base_url}{save_url}",
                json=json_payload,
                headers=headers,
            )
        except Exception as e:
            failed.append((sid, f"Error saving grade: {e}"))
            continue

        if resp.status_code != 200:
            failed.append(
                (sid, f"HTTP {resp.status_code}: {resp.text[:200]}")
            )
            continue

        new_score_value, unknown_ids = _compute_new_score(
            props, apply_ids, resolved_points
        )
        question_weight = props.get("question", {}).get("weight", "?")
        succeeded.append(
            (sid, _format_score(new_score_value), question_weight, unknown_ids)
        )

    lines = [
        f"## Batch grade result for question `{question_id}`",
        f"- **succeeded:** {len(succeeded)}",
        f"- **failed:** {len(failed)}",
        f"- **skipped (confidence < 0.6):** {len(skipped_confidence)}",
    ]
    if succeeded:
        lines.append("")
        lines.append("### Saved")
        rows_with_warnings: list[tuple[str, set[str]]] = []
        for sid, score, weight, unknown_ids in succeeded:
            lines.append(f"- `{sid}`: {score}/{weight}")
            if unknown_ids:
                rows_with_warnings.append((sid, unknown_ids))
        if rows_with_warnings:
            lines.append("")
            lines.append(
                "### ⚠️ Unknown rubric IDs (ignored locally; "
                "Gradescope may have rejected them)"
            )
            for sid, unknown_ids in rows_with_warnings:
                lines.append(f"- `{sid}`: {sorted(unknown_ids)}")
    if failed:
        lines.append("")
        lines.append("### Failed")
        for sid, err in failed:
            lines.append(f"- `{sid}`: {err}")
    if skipped_confidence:
        lines.append("")
        lines.append("### Skipped (low confidence)")
        for sid, cf in skipped_confidence:
            lines.append(f"- `{sid}`: confidence={cf:.2f}")
    return "\n".join(lines)


def create_rubric_item(
    course_id: str,
    question_id: str,
    description: str,
    weight: float,
    confirm_write: bool = False,
) -> str:
    """Create a new rubric item for a question.

    **WARNING**: This modifies the rubric. Changes apply to ALL submissions.

    Weight is always a **positive** number. Gradescope uses the question's
    ``scoring_type`` to determine interpretation:
    - **Positive scoring:** Weight = points earned (e.g., ``5.0`` → student
      gets +5 when this item is applied).
    - **Negative scoring:** Weight = points deducted (e.g., ``2.0`` → student
      loses −2 when this item is applied). The web UI shows this as ``-2``.

    **Do NOT pass negative weight values.** Gradescope expects positive
    numbers and handles the sign internally based on ``scoring_type``.

    Check the scoring type with ``get_question_rubric`` or
    ``get_submission_grading_context`` before creating items.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        description: Description of the rubric item (e.g., "Correct answer").
        weight: Point value — always positive. See scoring-type note above.
        confirm_write: Must be True to create the rubric item.
    """
    if not course_id or not question_id or not description:
        return "Error: course_id, question_id, and description are required."

    if not confirm_write:
        return write_confirmation_required(
            "create_rubric_item",
            [
                f"course_id=`{course_id}`",
                f"question_id=`{question_id}`",
                f"description={description}",
                f"weight={weight}",
            ],
        )

    try:
        conn = get_connection()
        # Need CSRF token from any grading page for this question
        # Get the first submission page
        grade_url = (
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/questions/{question_id}/submissions"
        )
        resp = conn.session.get(grade_url)
        if resp.status_code != 200:
            return f"Error accessing question page (status {resp.status_code})."

        soup = BeautifulSoup(resp.text, "html.parser")
        csrf_meta = soup.find("meta", {"name": "csrf-token"})
        csrf_token = csrf_meta.get("content", "") if csrf_meta else ""

    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error: {e}"

    # POST to rubric_item endpoint
    rubric_url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/questions/{question_id}/rubric_items"
    )

    payload = {
        "rubric_item[description]": description,
        "rubric_item[weight]": str(weight),
    }

    headers = {
        "X-CSRF-Token": csrf_token,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        resp = conn.session.post(rubric_url, data=payload, headers=headers)
    except Exception as e:
        return f"Error creating rubric item: {e}"

    if resp.status_code in (200, 201):
        try:
            result = resp.json()
            new_id = result.get("id", "?")
            return (
                f"✅ Rubric item created!\n"
                f"**ID:** `{new_id}`\n"
                f"**Description:** {description}\n"
                f"**Weight:** {weight}"
            )
        except Exception:
            return f"✅ Rubric item created (status {resp.status_code})."
    else:
        return f"Error: Failed to create rubric item (status {resp.status_code}). Response: {resp.text[:300]}"


def _find_question_submission_id(course_id: str, question_id: str) -> str:
    """Find a valid question submission ID by scraping the submissions page."""
    entries = _fetch_question_submission_entries(course_id, question_id)
    if not entries:
        raise ValueError(f"No submission found for question `{question_id}`.")
    return str(entries[0]["submission_id"])


def list_question_submissions(
    course_id: str, question_id: str, filter: str = "all",
) -> str:
    """List all Question Submission IDs for a question.

    This tool is essential for parallel grading: use it to pre-allocate
    specific submission IDs to subagents so they can grade independently
    without race conditions.

    Unlike ``get_assignment_submissions`` (which returns Global Submission
    IDs that grading tools cannot use), this returns **Question Submission
    IDs** that work directly with ``get_submission_grading_context`` and
    ``apply_grade``.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        filter: ``"all"`` (default), ``"ungraded"``, or ``"graded"``.

    Returns:
        JSON list of ``{submission_id, student_name, graded}`` entries
        for the requested question, sorted by submission ID.
    """
    if not course_id or not question_id:
        return "Error: course_id and question_id are required."

    if filter not in ("all", "ungraded", "graded"):
        return 'Error: filter must be "all", "ungraded", or "graded".'

    try:
        entries = _fetch_question_submission_entries(course_id, question_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error fetching submissions page: {e}"

    if not entries:
        return f"No submissions found for question `{question_id}`."

    # Apply filter
    if filter == "ungraded":
        entries = [e for e in entries if not e["graded"]]
    elif filter == "graded":
        entries = [e for e in entries if e["graded"]]

    # Sort by submission_id
    entries.sort(key=lambda e: int(e["submission_id"]))

    summary = (
        f"Found {len(entries)} {'(' + filter + ') ' if filter != 'all' else ''}"
        f"submissions for question `{question_id}`."
    )

    return json.dumps({"summary": summary, "submissions": entries}, indent=2)


def get_student_submission_map(
    course_id: str,
    assignment_id: str,
    student_name: str = "",
) -> str:
    """Build a per-student → {question_id: submission_id} map for an assignment.

    For each leaf question in the assignment outline (i.e. each gradable
    question, not the parent QuestionGroup), fetch the submission entries
    and group by student. Use this when reviewing several questions for one
    student — it replaces N calls to ``list_question_submissions`` plus a
    client-side join.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_name: Optional exact-match filter. When provided, the
            result only contains submissions for that student. Matching is
            case-sensitive against ``"First Last"`` exactly as Gradescope
            renders it on the submissions page.

    Returns:
        JSON with keys ``questions`` (the leaf questions in outline order),
        ``students`` (sorted alphabetically; each entry has ``name`` and a
        ``submissions`` mapping of ``question_id`` → ``submission_id``),
        and optionally ``errors`` (per-question fetch errors) or
        ``warning`` (when a ``student_name`` filter yields zero rows).

    The submission IDs returned are **Question Submission IDs**, ready to
    feed into ``get_submission_grading_context`` / ``apply_grade``.

    Limitation: students are keyed only by display name. If two students
    in the roster share a literal "First Last" string, their submissions
    will merge under one entry (last write wins per question). For
    disambiguation by email, use ``tool_get_student_assignment_link``
    which reads the scores CSV.
    """
    if not course_id or not assignment_id:
        return "Error: course_id and assignment_id are required."

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

    # Collect leaf questions in outline order. A "leaf" is any node that
    # holds submissions: children of a QuestionGroup, or a standalone
    # top-level question with no children.
    leaf_questions: list[dict] = []
    for group in tree:
        if group["children"]:
            for child in group["children"]:
                leaf_questions.append({
                    "qid": str(child["id"]),
                    "title": child.get("title") or "",
                    "weight": child.get("weight"),
                })
        elif group["id"] is not None:
            leaf_questions.append({
                "qid": str(group["id"]),
                "title": group.get("title") or "",
                "weight": group.get("weight"),
            })

    if not leaf_questions:
        return f"No leaf questions found for assignment `{assignment_id}`."

    by_student: dict[str, dict[str, str]] = {}
    errors: list[str] = []

    for q in leaf_questions:
        qid = q["qid"]
        try:
            entries = _fetch_question_submission_entries(course_id, qid)
        except AuthError as e:
            return f"Authentication error: {e}"
        except Exception as e:
            errors.append(f"question {qid}: {e}")
            continue

        for entry in entries:
            name = (entry.get("student_name") or "").strip()
            if not name:
                continue
            if student_name and name != student_name:
                continue
            by_student.setdefault(name, {})[qid] = entry["submission_id"]

    students_out = [
        {"name": name, "submissions": by_student[name]}
        for name in sorted(by_student.keys())
    ]

    result: dict = {
        "questions": leaf_questions,
        "students": students_out,
    }
    if errors:
        result["errors"] = errors
    if student_name and not students_out:
        result["warning"] = (
            f"No submissions found for student `{student_name}`. "
            f"Check spelling — match is case-sensitive against "
            f"'First Last' as Gradescope renders it."
        )

    return json.dumps(result, indent=2)


def get_next_ungraded(
    course_id: str, question_id: str, submission_id: str = "",
    output_format: str = "markdown",
) -> str:
    """Navigate to the next ungraded submission for the same question.

    Returns the grading context for the next ungraded submission,
    or a message if all submissions are graded.

    Args:
        course_id: The Gradescope course ID.
        question_id: The current question ID.
        submission_id: The current Question Submission ID (optional).
            If omitted or invalid, auto-discovers a valid submission.
            NOTE: This must be a Question Submission ID, not a Global
            Submission ID from get_assignment_submissions.
        output_format: "markdown" (default) or "json".
    """
    if not course_id or not question_id:
        return "Error: course_id and question_id are required."

    # Try the provided submission_id first; fall back to auto-discovery
    ctx = None
    if submission_id:
        try:
            ctx = _get_grading_context(course_id, question_id, submission_id)
        except ValueError as e:
            if "404" in str(e):
                # Likely a Global Submission ID — fall back to auto-discovery
                ctx = None
            else:
                return f"Error: {e}"
        except AuthError as e:
            return f"Authentication error: {e}"
        except Exception as e:
            return f"Error: {e}"

    if ctx is None:
        # Auto-discover a valid question submission ID
        try:
            auto_sid = _find_question_submission_id(course_id, question_id)
            ctx = _get_grading_context(course_id, question_id, auto_sid)
        except AuthError as e:
            return f"Authentication error: {e}"
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"

    props = ctx["props"]
    nav = props.get("navigation_urls", {})
    next_url = nav.get("next_ungraded", "")

    # Determine the current submission ID we're sitting on.
    # ALWAYS use the ID from the loaded context (props), not the original
    # input, because the input may be an invalid global submission ID that
    # was auto-discovered into a different question submission ID.
    current_sid = str(props.get("submission", {}).get("id", "")) or submission_id

    if not next_url:
        return _try_fallback_navigation(
            course_id, question_id, current_sid, props, output_format
        )

    # Extract IDs from the URL
    qid_m = re.search(r"/questions/(\d+)", next_url)
    sid_m = re.search(r"/submissions/(\d+)", next_url)

    if not qid_m or not sid_m:
        return f"Could not parse next ungraded URL: {next_url}"

    next_qid = qid_m.group(1)
    next_sid = sid_m.group(1)

    # Gradescope sets next_ungraded to the CURRENT submission when it is
    # itself ungraded.  The caller wants the NEXT one, so we must advance
    # past the current submission via next_submission.
    if next_sid == current_sid:
        next_sub_url = nav.get("next_submission", "")
        if not next_sub_url:
            return (
                "This is the only ungraded submission remaining for this "
                "question.  Grade it first, then call `get_next_ungraded` "
                "again to advance."
            )
        ns_qid_m = re.search(r"/questions/(\d+)", next_sub_url)
        ns_sid_m = re.search(r"/submissions/(\d+)", next_sub_url)
        if not ns_qid_m or not ns_sid_m:
            return f"Could not parse next_submission URL: {next_sub_url}"

        advance_qid = ns_qid_m.group(1)
        advance_sid = ns_sid_m.group(1)

        # Load the next submission to check whether it is ungraded
        try:
            advance_ctx = _get_grading_context(course_id, advance_qid, advance_sid)
        except Exception:
            # If we can't load it, just return it and let the caller deal
            return get_submission_grading_context(
                course_id, advance_qid, advance_sid, output_format
            )

        advance_sub = advance_ctx["props"].get("submission", {})
        if not advance_sub.get("graded", False):
            # It's ungraded — return this one
            return get_submission_grading_context(
                course_id, advance_qid, advance_sid, output_format
            )

        # It's graded — follow ITS next_ungraded (which should now
        # point to a genuinely different ungraded submission)
        adv_nav = advance_ctx["props"].get("navigation_urls", {})
        adv_next = adv_nav.get("next_ungraded", "")
        if adv_next:
            a_qid_m = re.search(r"/questions/(\d+)", adv_next)
            a_sid_m = re.search(r"/submissions/(\d+)", adv_next)
            if a_qid_m and a_sid_m:
                final_sid = a_sid_m.group(1)
                if final_sid != advance_sid:
                    return get_submission_grading_context(
                        course_id, a_qid_m.group(1), final_sid, output_format
                    )
        return _try_fallback_navigation(
            course_id, question_id, current_sid, props, output_format
        )

    # Normal case: next_ungraded points to a different submission
    return get_submission_grading_context(course_id, next_qid, next_sid, output_format)


def update_rubric_item(
    course_id: str,
    question_id: str,
    rubric_item_id: str,
    description: str | None = None,
    weight: float | None = None,
    confirm_write: bool = False,
) -> str:
    """Update an existing rubric item's description or weight.

    **WARNING**: Changes cascade to ALL submissions that have this item applied.
    Updating the weight will immediately change every affected student's score.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        rubric_item_id: The rubric item ID to update.
        description: New description, or None to keep unchanged.
        weight: New point value, or None to keep unchanged.
        confirm_write: Must be True to apply the update.
    """
    if not course_id or not question_id or not rubric_item_id:
        return "Error: course_id, question_id, and rubric_item_id are required."

    if description is None and weight is None:
        return "Error: at least one of description or weight must be provided."

    if not confirm_write:
        details = [
            f"course_id=`{course_id}`",
            f"question_id=`{question_id}`",
            f"rubric_item_id=`{rubric_item_id}`",
        ]
        if description is not None:
            details.append(f"new_description={description}")
        if weight is not None:
            details.append(f"new_weight={weight}")
        details.append("⚠️ This will affect ALL submissions with this rubric item applied.")
        return write_confirmation_required("update_rubric_item", details)

    try:
        conn = get_connection()
        # Get CSRF token from the question's submissions page
        subs_url = (
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/questions/{question_id}/submissions"
        )
        resp = conn.session.get(subs_url)
        if resp.status_code != 200:
            return f"Error accessing question page (status {resp.status_code})."

        soup = BeautifulSoup(resp.text, "html.parser")
        csrf_meta = soup.find("meta", {"name": "csrf-token"})
        csrf_token = csrf_meta.get("content", "") if csrf_meta else ""
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error: {e}"

    # PUT to the rubric item endpoint
    update_url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/questions/{question_id}/rubric_items/{rubric_item_id}"
    )

    payload = {}
    if description is not None:
        payload["rubric_item[description]"] = description
    if weight is not None:
        payload["rubric_item[weight]"] = str(weight)

    headers = {
        "X-CSRF-Token": csrf_token,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        resp = conn.session.put(update_url, data=payload, headers=headers)
    except Exception as e:
        return f"Error updating rubric item: {e}"

    if resp.status_code == 200:
        return (
            f"✅ Rubric item `{rubric_item_id}` updated!\n"
            f"**Description:** {description or '(unchanged)'}\n"
            f"**Weight:** {weight if weight is not None else '(unchanged)'}\n"
            f"⚠️ All submissions with this item have been recalculated."
        )
    else:
        return f"Error: Update failed (status {resp.status_code}). Response: {resp.text[:300]}"


def delete_rubric_item(
    course_id: str,
    question_id: str,
    rubric_item_id: str,
    confirm_write: bool = False,
) -> str:
    """Delete a rubric item from a question.

    **WARNING**: Deleting a rubric item removes it from ALL submissions.
    Any students who had this item applied will have their scores recalculated.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        rubric_item_id: The rubric item ID to delete.
        confirm_write: Must be True to delete the item.
    """
    if not course_id or not question_id or not rubric_item_id:
        return "Error: course_id, question_id, and rubric_item_id are required."

    if not confirm_write:
        return write_confirmation_required(
            "delete_rubric_item",
            [
                f"course_id=`{course_id}`",
                f"question_id=`{question_id}`",
                f"rubric_item_id=`{rubric_item_id}`",
                "⚠️ This permanently deletes the item and recalculates ALL affected scores.",
            ],
        )

    try:
        conn = get_connection()
        subs_url = (
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/questions/{question_id}/submissions"
        )
        resp = conn.session.get(subs_url)
        if resp.status_code != 200:
            return f"Error accessing question page (status {resp.status_code})."

        soup = BeautifulSoup(resp.text, "html.parser")
        csrf_meta = soup.find("meta", {"name": "csrf-token"})
        csrf_token = csrf_meta.get("content", "") if csrf_meta else ""
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error: {e}"

    delete_url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/questions/{question_id}/rubric_items/{rubric_item_id}"
    )

    headers = {
        "X-CSRF-Token": csrf_token,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        resp = conn.session.delete(delete_url, headers=headers)
    except Exception as e:
        return f"Error deleting rubric item: {e}"

    if resp.status_code in (200, 204):
        return (
            f"✅ Rubric item `{rubric_item_id}` deleted.\n"
            f"All affected submissions have been recalculated."
        )
    else:
        return f"Error: Delete failed (status {resp.status_code}). Response: {resp.text[:300]}"
