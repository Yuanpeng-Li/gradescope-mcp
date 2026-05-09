"""Higher-level grading workflow helpers.

These helpers make grading agents more reliable and context-efficient by:
1. Preparing a cached markdown artifact in /tmp/gradescope-mcp with prompt/rubric/reference notes.
2. Recommending a read strategy that prefers crop regions before whole-page reads.
3. Producing a coarse confidence score for whether auto-grading should proceed.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any
from bs4 import BeautifulSoup

from gradescope_mcp.cache import get_artifact_dir, get_artifact_path
from gradescope_mcp.auth import AuthError, get_connection
from gradescope_mcp.tools.grading import _get_outline_data
from gradescope_mcp.tools.grading_ops import _get_grading_context

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


def _fetch_assignment_questions(course_id: str, assignment_id: str) -> dict[str, dict]:
    """Fetch question metadata for an assignment from grade.json."""
    conn = get_connection()
    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/assignments/{assignment_id}/grade.json"
    )
    resp = conn.session.get(url)
    if resp.status_code != 200:
        raise ValueError(
            f"Cannot access grading dashboard for assignment `{assignment_id}` "
            f"(status {resp.status_code})."
        )

    data = resp.json()
    assignments = data.get("assignments", {})
    assignment = assignments.get(str(assignment_id), {})
    questions = assignment.get("questions", {})
    if not questions:
        raise ValueError(f"No questions found for assignment `{assignment_id}`.")
    return questions


def _resolve_assignment_questions(
    course_id: str,
    assignment_id: str | None,
    question_id: str,
) -> tuple[str, dict[str, dict], str | None]:
    """Resolve the assignment that owns a question."""
    normalized_assignment_id = str(assignment_id or "").strip()
    if normalized_assignment_id:
        questions = _fetch_assignment_questions(course_id, normalized_assignment_id)
        if str(question_id) in questions:
            return normalized_assignment_id, questions, None

    conn = get_connection()
    assignments = conn.account.get_assignments(course_id)
    for candidate in assignments:
        candidate_id = str(candidate.assignment_id)
        if candidate_id == normalized_assignment_id:
            continue
        try:
            questions = _fetch_assignment_questions(course_id, candidate_id)
        except Exception:
            continue
        if str(question_id) in questions:
            if normalized_assignment_id:
                note = (
                    f"question `{question_id}` was not found in assignment "
                    f"`{normalized_assignment_id}`; auto-resolved to "
                    f"`{candidate_id}`."
                )
            else:
                note = (
                    f"assignment_id not provided; auto-resolved question "
                    f"`{question_id}` to assignment `{candidate_id}`."
                )
            return candidate_id, questions, note

    if normalized_assignment_id:
        raise ValueError(
            f"question `{question_id}` was not found in assignment "
            f"`{normalized_assignment_id}` or any other assignment in course "
            f"`{course_id}`."
        )
    raise ValueError(
        f"Could not resolve an assignment for question `{question_id}` in "
        f"course `{course_id}`."
    )


def _build_question_label(question_id: str, questions: dict[str, dict]) -> str:
    """Build a human-readable question label like Q4.2 from dashboard metadata."""
    target = questions.get(str(question_id))
    if not target:
        return f"Q? ({question_id})"

    parent_id = target.get("parent_id")
    if parent_id and str(parent_id) in questions:
        parent = questions[str(parent_id)]
        return f"Q{parent.get('index', '?')}.{target.get('index', '?')}"
    return f"Q{target.get('index', '?')}"


def _find_first_submission_id(course_id: str, question_id: str) -> str:
    """Find the first available question submission id from the submissions page."""
    conn = get_connection()
    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/questions/{question_id}/submissions"
    )
    resp = conn.session.get(url)
    if resp.status_code != 200:
        raise ValueError(
            f"Cannot access submissions page for question `{question_id}` "
            f"(status {resp.status_code})."
        )

    match = re.search(
        rf"/courses/{course_id}/questions/{question_id}/submissions/(\d+)/grade",
        resp.text,
    )
    if not match:
        raise ValueError(f"No submission found for question `{question_id}`.")
    return match.group(1)


def _extract_outline_prompt_and_reference(
    course_id: str,
    assignment_id: str,
    question_id: str,
) -> tuple[str | None, str | None]:
    """Try to extract prompt and explanation/reference from outline data."""
    try:
        props = _get_outline_data(course_id, assignment_id)
    except Exception:
        return None, None

    question = props.get("questions", {}).get(str(question_id))
    if not question:
        return None, None

    prompt_parts: list[str] = []
    explanation_parts: list[str] = []
    for item in question.get("content", []):
        item_type = item.get("type")
        value = str(item.get("value", "")).strip()
        if not value:
            continue
        if item_type == "text":
            prompt_parts.append(value)
        elif item_type == "explanation":
            explanation_parts.append(value)

    prompt = "\n\n".join(prompt_parts).strip() or None
    explanation = "\n\n".join(explanation_parts).strip() or None
    return prompt, explanation


def _extract_rubric_summary(props: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract a compact rubric representation from grading props."""
    items = []
    for item in props.get("rubric_items", []):
        items.append(
            {
                "id": str(item.get("id", "")),
                "description": str(item.get("description", "")).strip(),
                "weight": item.get("weight"),
            }
        )
    return items


def _draft_reference_from_rubric(rubric_items: list[dict[str, Any]]) -> str:
    """Generate a fallback reference answer draft from rubric descriptions."""
    if not rubric_items:
        return (
            "No rubric items were available. Draft a reference answer manually "
            "before grading."
        )

    correct_items = [
        item for item in rubric_items
        if "correct" in item["description"].lower() and item["description"]
    ]
    partial_items = [
        item for item in rubric_items
        if item not in correct_items and item["description"]
    ]

    lines = [
        "This is a fallback reference-answer draft synthesized from the rubric.",
        "Use it as guidance only; verify against the scanned prompt and student work.",
    ]
    if correct_items:
        lines.append("")
        lines.append("Expected full-credit elements:")
        for item in correct_items:
            lines.append(f"- {item['description']}")

    if partial_items:
        lines.append("")
        lines.append("Common issues to watch for:")
        for item in partial_items[:8]:
            lines.append(f"- {item['description']}")

    return "\n".join(lines)


def _format_crop_regions(crop_rects: list[dict[str, Any]]) -> list[str]:
    """Format crop rectangles for human-readable markdown output."""
    lines = []
    for rect in crop_rects:
        lines.append(
            "- page {page}: x={x1}%..{x2}%, y={y1}%..{y2}%".format(
                page=rect.get("page_number", "?"),
                x1=rect.get("x1", "?"),
                x2=rect.get("x2", "?"),
                y1=rect.get("y1", "?"),
                y2=rect.get("y2", "?"),
            )
        )
    return lines


def _select_relevant_pages(
    pages: list[dict[str, Any]],
    crop_rects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only crop pages and their immediate neighbors.

    When no crop info is available — common for online assignments and for
    scanned exams whose outline lacks crop rects — returning the first three
    pages silently hides any answer beyond page 3 of a multi-page submission.
    Return everything in that case so the caller can decide.

    Same fallback when crop pages don't intersect the actual submission pages
    at all (e.g., student tagged the wrong region): return everything rather
    than guess.
    """
    if not pages:
        return []

    crop_page_numbers = {
        rect.get("page_number")
        for rect in crop_rects
        if rect.get("page_number") is not None
    }
    if not crop_page_numbers:
        return list(pages)

    wanted = set()
    for page_number in crop_page_numbers:
        wanted.update({page_number - 1, page_number, page_number + 1})

    filtered = [
        page for page in pages
        if page.get("number") in wanted
    ]
    return filtered or list(pages)


def _compute_readiness(
    prompt_text: str | None,
    reference_answer: str | None,
    crop_rects: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    rubric_items: list[dict[str, Any]] | None = None,
) -> tuple[float, list[str], str]:
    """Compute a readiness score: do we have enough context to START grading?

    This is NOT the same as grading confidence. Readiness checks whether we
    have the question text, reference answers, crop regions, etc. Grading
    confidence can only be determined by the AI agent AFTER reading the
    student's actual submission.

    Returns (score, reasons, action).
    """
    score = 0.25
    reasons: list[str] = []

    if prompt_text:
        score += 0.35
        reasons.append("Structured prompt text is available.")
    elif crop_rects and pages:
        score += 0.15
        reasons.append(
            "No structured prompt text, but scanned crop/page context is available."
        )
    else:
        reasons.append("Prompt text is unavailable; grading depends on scanned pages.")

    if reference_answer:
        score += 0.2
        reasons.append("Reference answer or explanation is available.")
    elif rubric_items:
        score += 0.15
        reasons.append(
            "No structured reference answer, but rubric items are available for manual grading."
        )
    else:
        reasons.append(
            "No reference answer was found. This is expected for scanned "
            "PDF / handwritten assignments — use rubric-only grading."
        )

    if crop_rects:
        score += 0.1
        reasons.append("Question crop coordinates are available for targeted reading.")
    else:
        reasons.append("No crop coordinates found; must inspect whole pages.")

    if len(pages) <= 2:
        score += 0.1
        reasons.append("Few relevant pages reduce ambiguity.")
    elif len(pages) >= 5:
        reasons.append("Many pages increase the chance of cross-page spillover.")

    # Penalties for complex submissions
    if any((rect.get("y2", 0) - rect.get("y1", 0)) > 30 for rect in crop_rects):
        reasons.append("Large crop height suggests the answer may span more than one logical block.")
        score -= 0.05
    if len(pages) >= 8:
        reasons.append("This submission has many scanned pages, so cross-page spillover is more likely.")
        score -= 0.05

    bounded = max(0.0, min(score, 0.95))
    if bounded >= 0.8:
        action = "ready"
    elif bounded >= 0.55:
        action = "partially_ready"
    else:
        action = "not_ready"
    return bounded, reasons, action


def prepare_grading_artifact(
    course_id: str,
    assignment_id: str | None,
    question_id: str,
    submission_id: str | None = None,
) -> str:
    """Prepare a cached markdown artifact in /tmp/gradescope-mcp for an assignment question.

    The artifact includes question metadata, prompt text when available, rubric,
    a reference answer or fallback draft, and read-strategy notes for agents.
    """
    if not course_id or not question_id:
        return "Error: course_id and question_id are required."

    try:
        assignment_id, questions, resolution_note = _resolve_assignment_questions(
            course_id, assignment_id, question_id
        )
        target = questions.get(str(question_id), {})

        if submission_id is None:
            submission_id = _find_first_submission_id(course_id, question_id)

        ctx = _get_grading_context(course_id, question_id, submission_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error preparing grading artifact: {e}"

    props = ctx["props"]
    question = props.get("question", {})
    prompt_text, explanation = _extract_outline_prompt_and_reference(
        course_id, assignment_id, question_id
    )
    rubric_items = _extract_rubric_summary(props)
    # Only use the real explanation for readiness scoring — NOT the rubric
    # fallback.  The rubric draft is still included in the artifact text for
    # guidance, but it must not inflate the readiness score.
    rubric_draft = _draft_reference_from_rubric(rubric_items) if not explanation else None
    parameters = question.get("parameters") or {}
    crop_rects = parameters.get("crop_rect_list", [])
    pages = [
        page for page in props.get("pages", [])
        if isinstance(page, dict) and page.get("url")
        and not _is_placeholder_page(page)
    ]
    relevant_pages = _select_relevant_pages(pages, crop_rects)

    readiness, reasons, action = _compute_readiness(
        prompt_text, explanation, crop_rects, relevant_pages, rubric_items
    )
    question_label = _build_question_label(question_id, questions)

    artifact_path = get_artifact_path(
        f"gradescope-grading-{assignment_id}-{question_id}.md"
    )

    lines = [
        f"# Grading Artifact: {question_label}",
        "",
        "## Metadata",
        f"- course_id: `{course_id}`",
        f"- assignment_id: `{assignment_id}`",
        f"- question_id: `{question_id}`",
        f"- sample_submission_id: `{submission_id}`",
        f"- weight: `{question.get('weight', target.get('weight', '?'))}`",
        f"- question_type: `{question.get('type', target.get('type', 'Unknown'))}`",
    ]
    if resolution_note:
        lines.append(f"- resolution: {resolution_note}")
    lines.extend(
        [
            "",
            "## Prompt",
            prompt_text or (
                "Prompt text is not available from Gradescope's structured data. "
                "Use the crop regions and page URLs below to inspect the scanned prompt."
            ),
            "",
            "## Rubric",
        ]
    )

    if rubric_items:
        for item in rubric_items:
            lines.append(
                f"- `{item['id']}` ({item['weight']} pts): {item['description'] or '(no description)'}"
            )
    else:
        lines.append("- No rubric items found.")

    # Show the reference answer section with clear labeling
    if explanation:
        ref_section_title = "## Reference Answer"
        ref_section_body = explanation
    elif rubric_draft:
        ref_section_title = "## Reference Answer (⚠️ Rubric-Based Fallback)"
        ref_section_body = rubric_draft
    else:
        ref_section_title = "## Reference Answer"
        ref_section_body = (
            "No reference answer is available. For scanned PDF / handwritten "
            "assignments, this is expected — grade based on the rubric items above."
        )

    lines.extend(
        [
            "",
            ref_section_title,
            ref_section_body,
            "",
            "## Read Strategy",
            "- Start with the crop region only.",
            "- If handwriting exits the crop boundary or the reasoning appears truncated, read the whole page.",
            "- If the answer still appears incomplete, inspect the previous and next page before grading.",
        ]
    )

    if crop_rects:
        lines.append("")
        lines.append("### Crop Regions")
        lines.extend(_format_crop_regions(crop_rects))

    if relevant_pages:
        lines.append("")
        lines.append("### Relevant Pages")
        for page in relevant_pages:
            lines.append(f"- page {page.get('number', '?')}: {page['url']}")

    lines.extend(
        [
            "",
            "## Readiness Assessment",
            f"- readiness: `{readiness:.2f}`",
            f"- status: `{action}`",
        ]
    )
    for reason in reasons:
        lines.append(f"- {reason}")

    lines.extend(
        [
            "",
            "## Grading Confidence (Agent Self-Report)",
            "After reading the student's answer, YOU (the agent) must assess:",
            "- **confidence**: a float 0.0-1.0 representing how sure you are about your grade",
            "- Pass this as the `confidence` parameter when calling `tool_apply_grade`",
            "- If confidence < 0.6: skip this submission and flag for human review",
            "- If confidence 0.6-0.8: grade but present to user for confirmation first",
            "- If confidence > 0.8: safe to auto-grade",
        ]
    )

    artifact_path.write_text("\n".join(lines), encoding="utf-8")
    summary = [
        f"Prepared grading artifact for {question_label}.",
        f"- Path: `{artifact_path}`",
    ]
    if resolution_note:
        summary.append(f"- Resolution: {resolution_note}")
    summary.extend(
        [
            f"- Readiness: `{readiness:.2f}` ({action})",
            "- **Remember:** After reading each submission, self-report your "
            "grading confidence via the `confidence` param in `tool_apply_grade`.",
        ]
    )
    return "\n".join(summary)


def assess_submission_readiness(
    course_id: str,
    assignment_id: str | None,
    question_id: str,
    submission_id: str,
) -> str:
    """Assess how safely an agent can auto-grade a specific submission.

    Returns the preferred read order, page/crop hints, and a confidence score
    that can be used to skip or escalate uncertain submissions.
    """
    if not course_id or not question_id or not submission_id:
        return (
            "Error: course_id, question_id, and submission_id "
            "are required."
        )

    try:
        assignment_id, questions, resolution_note = _resolve_assignment_questions(
            course_id, assignment_id, question_id
        )
        ctx = _get_grading_context(course_id, question_id, submission_id)
        prompt_text, explanation = _extract_outline_prompt_and_reference(
            course_id, assignment_id, question_id
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error assessing submission readiness: {e}"

    props = ctx["props"]
    question = props.get("question", {})
    parameters = question.get("parameters") or {}
    crop_rects = parameters.get("crop_rect_list", [])
    pages = [
        page for page in props.get("pages", [])
        if isinstance(page, dict) and page.get("url")
    ]
    relevant_pages = _select_relevant_pages(pages, crop_rects)
    reference_answer = explanation or None
    readiness, reasons, action = _compute_readiness(
        prompt_text, reference_answer, crop_rects, relevant_pages,
        props.get("rubric_items", []),
    )
    question_label = _build_question_label(question_id, questions)

    strategy = [
        "1. Read the crop region only.",
        "2. If the crop looks truncated or handwriting crosses the border, read the whole page.",
        "3. If the reasoning still looks incomplete, inspect the previous and next page.",
    ]

    lines = [
        f"## Readiness Assessment — {question_label}",
        f"- assignment_id: `{assignment_id}`",
        f"- submission_id: `{submission_id}`",
        f"- readiness: `{readiness:.2f}`",
        f"- status: `{action}`",
        "",
        "### Read Order",
    ]
    if resolution_note:
        lines.append(f"- resolution: {resolution_note}")
    lines.extend(f"- {step}" for step in strategy)

    if crop_rects:
        lines.append("")
        lines.append("### Crop Regions")
        lines.extend(_format_crop_regions(crop_rects))

    if relevant_pages:
        lines.append("")
        lines.append("### Page URLs")
        for page in relevant_pages:
            lines.append(f"- page {page.get('number', '?')}: {page['url']}")

    lines.append("")
    lines.append("### Readiness Notes")
    for reason in reasons:
        lines.append(f"- {reason}")

    return "\n".join(lines)


def cache_relevant_pages(
    course_id: str,
    assignment_id: str | None,
    question_id: str,
    submission_id: str,
    include_all_pages: bool = False,
) -> str:
    """Download the crop page and its neighbors to /tmp/gradescope-mcp for local inspection.

    Set ``include_all_pages=True`` to download every page of the submission,
    bypassing the crop-centric filter. Use this when the student tagged the
    wrong page for the current question and the work is on a page that the
    normal (tag-aware) filter would miss.
    """
    if not course_id or not question_id or not submission_id:
        return (
            "Error: course_id, question_id, and submission_id "
            "are required."
        )

    try:
        assignment_id, _questions, _resolution_note = _resolve_assignment_questions(
            course_id, assignment_id, question_id
        )
        ctx = _get_grading_context(course_id, question_id, submission_id)
        conn = get_connection()
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error caching relevant pages: {e}"

    props = ctx["props"]
    question = props.get("question", {})
    parameters = question.get("parameters") or {}
    crop_rects = parameters.get("crop_rect_list", [])
    pages = [
        page for page in props.get("pages", [])
        if isinstance(page, dict) and page.get("url")
    ]
    if include_all_pages:
        relevant_pages = pages
    else:
        relevant_pages = _select_relevant_pages(pages, crop_rects)
    if not relevant_pages:
        return "No relevant pages were found for this submission."

    out_dir = get_artifact_dir(
        f"gradescope-pages-{assignment_id}-{question_id}-{submission_id}"
    )

    saved_paths = []
    for page in relevant_pages:
        page_number = page.get("number", "unknown")
        out_path = out_dir / f"page_{page_number}.jpg"
        response = conn.session.get(_normalize_url(page["url"]))
        response.raise_for_status()
        with open(out_path, "wb") as handle:
            handle.write(response.content)
        saved_paths.append(out_path)

    lines = [
        f"Cached {len(saved_paths)} relevant page(s) for question `{question_id}`.",
        f"- Directory: `{out_dir}`",
    ]
    for path in saved_paths:
        lines.append(f"- `{path}`")
    return "\n".join(lines)


def prepare_answer_key(course_id: str, assignment_id: str) -> str:
    """Prepare an assignment-wide grading basis artifact.

    Extracts ALL questions from the assignment outline, including:
    - Question numbers, types, and weights
    - Prompt/question text (if available in structured data)
    - Explanation/reference answers (if provided by the instructor)
    - Explicit missing-answer markers when no instructor reference exists

    Saves the result to /tmp/gradescope-mcp/gradescope-answerkey-{assignment_id}.md.
    This file can then be referenced when grading individual submissions
    without implying that every question has a true answer key.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: course_id and assignment_id are required."

    try:
        questions = _fetch_assignment_questions(course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error preparing answer key: {e}"

    # Outline data is optional (scanned exams don't have AssignmentEditor)
    try:
        outline_props = _get_outline_data(course_id, assignment_id)
    except Exception:
        outline_props = {}

    outline_questions = outline_props.get("questions", {})
    assignment_info = outline_props.get("assignment", {})
    title = assignment_info.get("title", f"Assignment {assignment_id}")

    # Build a sorted list of questions
    question_list = []
    for qid, q in questions.items():
        parent_id = q.get("parent_id")
        q_data = {
            "id": qid,
            "title": q.get("title", ""),
            "weight": q.get("weight", 0),
            "type": q.get("type", "Unknown"),
            "parent_id": parent_id,
            "index": q.get("index", 0),
        }

        # Build label
        if parent_id and str(parent_id) in questions:
            parent = questions[str(parent_id)]
            q_data["label"] = f"Q{parent.get('index', '?')}.{q.get('index', '?')}"
        else:
            q_data["label"] = f"Q{q.get('index', '?')}"

        # Extract prompt text and explanation from outline
        outline_q = outline_questions.get(str(qid), {})
        prompt_parts = []
        explanation_parts = []
        for item in outline_q.get("content", []):
            item_type = item.get("type")
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            if item_type == "text":
                prompt_parts.append(value)
            elif item_type == "explanation":
                explanation_parts.append(value)

        q_data["prompt"] = "\n\n".join(prompt_parts).strip() or None
        q_data["explanation"] = "\n\n".join(explanation_parts).strip() or None

        # Only include leaf questions (with weight > 0)
        if q_data["weight"] and float(q_data["weight"]) > 0:
            question_list.append(q_data)

    # Sort by label
    question_list.sort(key=lambda x: (x.get("parent_id") or 0, x["index"]))

    # Build markdown
    lines = [
        f"# Grading Basis: {title}",
        f"",
        f"- **course_id:** `{course_id}`",
        f"- **assignment_id:** `{assignment_id}`",
        f"- **Total questions:** {len(question_list)}",
        "",
    ]

    missing_answers = []

    for q in question_list:
        lines.append(f"---")
        lines.append(f"## {q['label']}: {q['title']} ({q['weight']} pts)")
        lines.append(f"- question_id: `{q['id']}`")
        lines.append(f"- type: `{q['type']}`")
        lines.append("")

        if q["prompt"]:
            lines.append("### Question")
            lines.append(q["prompt"])
            lines.append("")

        if q["explanation"]:
            lines.append("### Reference Answer")
            lines.append(q["explanation"])
            lines.append("")
        else:
            missing_answers.append(q["label"])
            lines.append("### Reference Status")
            lines.append(
                "⚠️ No instructor-provided reference answer is available for this question. "
                "This is typical for scanned PDF / handwritten assignments. "
                "Do not treat this file as a true answer key here; use the rubric items, prompt, "
                "and scanned pages as your grading basis."
            )
            lines.append("")

    # Summary at the top
    if missing_answers:
        lines.insert(
            6,
            f"- **⚠️ Missing answers:** {', '.join(missing_answers)}\n",
        )

    artifact_path = get_artifact_path(f"gradescope-answerkey-{assignment_id}.md")
    artifact_path.write_text("\n".join(lines), encoding="utf-8")

    covered_answers = len(question_list) - len(missing_answers)

    return (
        f"✅ Grading basis prepared for **{title}**\n"
        f"- Path: `{artifact_path}`\n"
        f"- Questions: {len(question_list)}\n"
        f"- Questions with instructor reference answers: {covered_answers}\n"
        f"- Missing reference answers: {len(missing_answers)} ({', '.join(missing_answers) or 'none'})\n\n"
        f"Use this file as context when grading submissions. Missing-answer entries are placeholders, not true answer keys."
    )


def smart_read_submission(
    course_id: str,
    assignment_id: str | None,
    question_id: str,
    submission_id: str,
) -> str:
    """Get a smart, tiered reading plan for a student's submission.

    Returns page image URLs in priority order:
    1. **Tier 1 (Crop Only):** The crop region URLs for the question's designated area.
       Agent should read ONLY this first. If the answer is fully contained, grade it.
    2. **Tier 2 (Full Page):** If handwriting exits the crop boundary or reasoning
       appears truncated, read the full page(s) containing the crop.
    3. **Tier 3 (Adjacent Pages):** If the answer still appears incomplete, read the
       previous and next pages.

    Also returns the confidence score to decide whether to auto-grade or skip.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        question_id: The question ID.
        submission_id: The question submission ID.
    """
    if not course_id or not question_id or not submission_id:
        return "Error: course_id, question_id, and submission_id are required."

    try:
        assignment_id, questions, resolution_note = _resolve_assignment_questions(
            course_id, assignment_id, question_id
        )
        ctx = _get_grading_context(course_id, question_id, submission_id)
        prompt_text, explanation = _extract_outline_prompt_and_reference(
            course_id, assignment_id, question_id,
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except (ValueError, Exception) as e:
        return f"Error: {e}"

    props = ctx["props"]
    question = props.get("question", {})
    submission = props.get("submission", {})
    parameters = question.get("parameters") or {}
    crop_rects = parameters.get("crop_rect_list", [])

    pages = [
        p for p in props.get("pages", [])
        if isinstance(p, dict) and p.get("url")
    ]
    page_by_number = {p.get("number"): p for p in pages}

    question_label = _build_question_label(question_id, questions)

    # Compute readiness (pre-read context check, NOT grading confidence)
    reference = explanation or None
    readiness, reasons, action = _compute_readiness(
        prompt_text, reference, crop_rects, pages, props.get("rubric_items", []),
    )

    lines = [
        f"## Smart Read Plan — {question_label}",
        f"**Student:** {submission.get('owner_names', 'Unknown')}",
        f"**Assignment ID:** `{assignment_id}`",
        f"**Weight:** {question.get('weight', '?')} pts",
        f"**Readiness:** `{readiness:.2f}` → `{action}`",
        "",
    ]
    if resolution_note:
        lines.append(f"**Resolution:** {resolution_note}")
        lines.append("")

    # Tier 1: Crop pages only
    crop_page_numbers = sorted(set(
        r.get("page_number") for r in crop_rects if r.get("page_number") is not None
    ))

    if crop_page_numbers:
        lines.append("### Tier 1 — Crop Region (read this FIRST)")
        lines.append("Read ONLY the crop area. If the answer is fully within the box, grade it.")
        for cr in crop_rects:
            pn = cr.get("page_number", "?")
            lines.append(
                f"- Page {pn}: crop x={cr.get('x1','?')}%-{cr.get('x2','?')}%, "
                f"y={cr.get('y1','?')}%-{cr.get('y2','?')}%"
            )
        for pn in crop_page_numbers:
            p = page_by_number.get(pn)
            if p:
                lines.append(f"- 📄 Page {pn} URL: {p['url']}")
        lines.append("")

        # Tier 2: Full pages containing crop
        lines.append("### Tier 2 — Full Page (if answer overflows crop)")
        lines.append("If handwriting exits the crop box or reasoning is truncated:")
        for pn in crop_page_numbers:
            p = page_by_number.get(pn)
            if p:
                lines.append(f"- 📄 Full page {pn}: {p['url']}")
        lines.append("")

        # Tier 3: Adjacent pages
        adjacent_numbers = set()
        for pn in crop_page_numbers:
            adjacent_numbers.add(pn - 1)
            adjacent_numbers.add(pn + 1)
        adjacent_numbers -= set(crop_page_numbers)

        adjacent_pages = [
            (n, page_by_number[n]) for n in sorted(adjacent_numbers)
            if n in page_by_number
        ]

        if adjacent_pages:
            lines.append("### Tier 3 — Adjacent Pages (if answer still incomplete)")
            lines.append("Check these if student's work continues beyond the designated area:")
            for pn, p in adjacent_pages:
                lines.append(f"- 📄 Page {pn}: {p['url']}")
            lines.append("")
    else:
        # No crop regions — provide all pages
        lines.append("### No Crop Regions Available")
        lines.append("Read all available pages to find the student's answer:")
        for p in pages[:5]:
            lines.append(f"- 📄 Page {p.get('number', '?')}: {p['url']}")
        if len(pages) > 5:
            lines.append(f"- _...and {len(pages) - 5} more pages_")
        lines.append("")

    # Readiness notes
    lines.append("### Readiness Assessment")
    for reason in reasons:
        lines.append(f"- {reason}")
    lines.append("")

    if action == "not_ready":
        lines.append(
            "⚠️ **NOT READY** — Even after using the scanned pages, the available context is thin. "
            "Escalate or request human review."
        )
    elif action == "partially_ready":
        lines.append(
            "⚡ **PARTIALLY READY** — Some context is missing. Proceed with caution."
        )
    else:
        lines.append(
            "✅ **READY** — All key context available. Good to start reading."
        )

    lines.extend(
        [
            "",
            "### Grading Confidence (Your Responsibility)",
            "After reading the student's answer, assess your own grading confidence:",
            "- **confidence 0.0-0.6**: Skip, flag for human review",
            "- **confidence 0.6-0.8**: Grade but request user confirmation",
            "- **confidence 0.8-1.0**: Safe to auto-grade",
            "- Pass your confidence score as `confidence` in `tool_apply_grade`.",
        ]
    )

    # Answer key reference
    answer_key_path = get_artifact_path(f"gradescope-answerkey-{assignment_id}.md")
    if answer_key_path.exists():
        lines.append(f"\n📚 **Answer key available:** `{answer_key_path}`")
    else:
        lines.append(
            f"\n📚 **No answer key cached.** Run `prepare_answer_key` first for "
            f"context-efficient grading."
        )

    return "\n".join(lines)
