"""Gradescope MCP Server definition.

Registers all tools, resources, and prompts with the MCP server.
"""

import json
import logging

from mcp.server.fastmcp import FastMCP

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.courses import list_courses, get_course_roster
from gradescope_mcp.tools.assignments import (
    get_assignments,
    get_assignment_details,
    modify_assignment_dates,
    rename_assignment,
)
from gradescope_mcp.tools.submissions import (
    upload_submission,
    get_assignment_submissions,
    get_student_submission,
    get_assignment_graders,
)
from gradescope_mcp.tools.extensions import get_extensions, set_extension
from gradescope_mcp.tools.grading import (
    get_assignment_outline,
    export_assignment_scores,
    get_grading_progress,
    get_student_assignment_link,
)
from gradescope_mcp.tools.regrades import (
    get_regrade_requests,
    get_regrade_detail,
)
from gradescope_mcp.tools.statistics import get_assignment_statistics
from gradescope_mcp.tools.grading_ops import (
    get_submission_grading_context,
    apply_grade,
    apply_grade_batch,
    create_rubric_item,
    update_rubric_item,
    delete_rubric_item,
    get_next_ungraded,
    get_question_rubric,
    list_question_submissions,
    get_student_submission_map,
)
from gradescope_mcp.tools.answer_groups import (
    get_answer_groups,
    get_answer_group_detail,
    grade_answer_group,
)
from gradescope_mcp.tools.grading_workflow import (
    prepare_grading_artifact,
    assess_submission_readiness,
    cache_relevant_pages,
    prepare_answer_key,
    smart_read_submission,
)

logger = logging.getLogger(__name__)

# Create the MCP server
mcp = FastMCP("Gradescope MCP Server")

# ============================================================
# Tools
# ============================================================


@mcp.tool()
def tool_list_courses() -> str:
    """List all Gradescope courses for the authenticated user.

    Returns courses grouped by role (instructor vs student),
    including course ID, name, semester, and assignment count.
    """
    return list_courses()


@mcp.tool()
def tool_get_assignments(course_id: str) -> str:
    """Get all assignments for a specific Gradescope course.

    Returns a table of assignments with names, IDs, dates, status, and grades.

    Args:
        course_id: The Gradescope course ID (found via list_courses).
    """
    return get_assignments(course_id)


@mcp.tool()
def tool_get_assignment_details(course_id: str, assignment_id: str) -> str:
    """Get detailed information about a specific assignment.

    Returns the assignment name, dates, submission status, and grade.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID (found via get_assignments).
    """
    return get_assignment_details(course_id, assignment_id)


@mcp.tool()
def tool_get_course_roster(course_id: str) -> str:
    """Get the full roster (students, TAs, instructors) for a course.

    Returns a table grouped by role with name, email, SID, and submission count.
    Requires instructor or TA access to the course.

    Args:
        course_id: The Gradescope course ID.
    """
    return get_course_roster(course_id)


@mcp.tool()
def tool_upload_submission(
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
    return upload_submission(
        course_id, assignment_id, file_paths, leaderboard_name, confirm_write
    )


@mcp.tool()
def tool_get_extensions(course_id: str, assignment_id: str) -> str:
    """Get all student extensions for a specific assignment.

    Returns a table of extensions with user ID, name, and modified dates.
    Requires instructor or TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    return get_extensions(course_id, assignment_id)


@mcp.tool()
def tool_set_extension(
    course_id: str,
    assignment_id: str,
    user_id: str,
    release_date: str | None = None,
    due_date: str | None = None,
    late_due_date: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Add or update an extension for a student on an assignment.

    If the student already has an extension, it will be overwritten.
    At least one date must be provided. Dates must be in order:
    release_date <= due_date <= late_due_date.

    Requires instructor or TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        user_id: The student's Gradescope user ID (found via get_course_roster).
        release_date: Extension release date (ISO format: YYYY-MM-DDTHH:MM).
        due_date: Extension due date (ISO format: YYYY-MM-DDTHH:MM).
        late_due_date: Extension late due date (ISO format: YYYY-MM-DDTHH:MM).
        confirm_write: Must be True to apply the extension update.
    """
    return set_extension(
        course_id,
        assignment_id,
        user_id,
        release_date,
        due_date,
        late_due_date,
        confirm_write,
    )


@mcp.tool()
def tool_modify_assignment_dates(
    course_id: str,
    assignment_id: str,
    release_date: str | None = None,
    due_date: str | None = None,
    late_due_date: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Modify the dates of an assignment (release, due, late due).

    At least one date must be provided. Only the provided dates will be changed.
    Requires instructor or TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        release_date: New release date (ISO format: YYYY-MM-DDTHH:MM), or None to keep unchanged.
        due_date: New due date (ISO format: YYYY-MM-DDTHH:MM), or None to keep unchanged.
        late_due_date: New late due date (ISO format: YYYY-MM-DDTHH:MM), or None to keep unchanged.
        confirm_write: Must be True to apply the date change.
    """
    return modify_assignment_dates(
        course_id,
        assignment_id,
        release_date,
        due_date,
        late_due_date,
        confirm_write,
    )


@mcp.tool()
def tool_rename_assignment(
    course_id: str,
    assignment_id: str,
    new_title: str,
    confirm_write: bool = False,
) -> str:
    """Rename an assignment on Gradescope.

    Requires instructor or TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        new_title: The new title for the assignment.
        confirm_write: Must be True to perform the rename.
    """
    return rename_assignment(course_id, assignment_id, new_title, confirm_write)


@mcp.tool()
def tool_get_assignment_submissions(
    course_id: str, assignment_id: str
) -> str:
    """Get all submissions for an assignment (instructor/TA only).

    Returns a list of submission IDs and file counts.
    Note: May be slow for large classes as it fetches each submission individually.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    return get_assignment_submissions(course_id, assignment_id)


@mcp.tool()
def tool_get_student_submission(
    course_id: str, assignment_id: str, student_email: str
) -> str:
    """Get a specific student's most recent submission (instructor/TA only).

    Returns links to the submission files.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_email: The student's email address.
    """
    return get_student_submission(course_id, assignment_id, student_email)


@mcp.tool()
def tool_get_assignment_graders(
    course_id: str, question_id: str
) -> str:
    """Get the list of graders assigned to a specific question (instructor/TA only).

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID within the assignment.
    """
    return get_assignment_graders(course_id, question_id)


@mcp.tool()
def tool_get_assignment_outline(course_id: str, assignment_id: str) -> str:
    """Get the question/rubric outline for an assignment.

    Returns the hierarchical question structure with IDs, types, weights, and
    question text. Essential for understanding how an assignment is structured.
    Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    return get_assignment_outline(course_id, assignment_id)


@mcp.tool()
def tool_export_assignment_scores(
    course_id: str,
    assignment_id: str,
    output_format: str = "markdown",
) -> str:
    """Export per-question scores for an assignment.

    Markdown form truncates to the first 20 students (use for spot-check);
    JSON form returns the full roster plus per-student per-question scores.
    Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        output_format: ``"markdown"`` (default) or ``"json"``. Use
            ``"json"`` whenever you need every row or per-question scores.
    """
    return export_assignment_scores(course_id, assignment_id, output_format)


@mcp.tool()
def tool_get_student_assignment_link(
    course_id: str,
    assignment_id: str,
    student_name: str,
) -> str:
    """Return the per-student `/assignments/.../submissions/Z` URL.

    Opens the entire assignment submission (cover-sheet view) for one
    student. Use this for skim-review across all questions — different
    from the per-question `/questions/.../grade` URL.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_name: Exact-match "First Last" (case-sensitive, matches
            the Gradescope scores CSV columns).
    """
    return get_student_assignment_link(course_id, assignment_id, student_name)


@mcp.tool()
def tool_get_grading_progress(course_id: str, assignment_id: str) -> str:
    """Get the grading progress dashboard for an assignment.

    Shows each question's grading status: how many submissions have been graded,
    assigned graders, and completion percentage. Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    return get_grading_progress(course_id, assignment_id)


@mcp.tool()
def tool_get_regrade_requests(course_id: str, assignment_id: str) -> str:
    """List all regrade requests for an assignment.

    Returns a table of pending and completed regrade requests with student name,
    question, grader, status, and identifiers for fetching details.
    Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    return get_regrade_requests(course_id, assignment_id)


@mcp.tool()
def tool_get_regrade_detail(
    course_id: str, question_id: str, submission_id: str
) -> str:
    """Get detailed information about a specific regrade request.

    Shows the student's regrade message, the current rubric, applied rubric items,
    the grader's response (if any), and submission page links. Use question_id and
    submission_id from the regrade request listing.
    Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID (from get_regrade_requests).
        submission_id: The submission ID (from get_regrade_requests).
    """
    return get_regrade_detail(course_id, question_id, submission_id)


@mcp.tool()
def tool_get_assignment_statistics(course_id: str, assignment_id: str) -> str:
    """Get comprehensive statistics for an assignment.

    Returns assignment-level summary (mean, median, min/max, std dev) and
    per-question breakdowns. Highlights low-scoring questions.
    Requires instructor/TA access.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    return get_assignment_statistics(course_id, assignment_id)


@mcp.tool()
def tool_get_submission_grading_context(
    course_id: str, question_id: str, submission_id: str
) -> str:
    """Get full grading context for a question submission.

    Returns current rubric items (with IDs), applied evaluations, score,
    comments, point adjustment, navigation URLs (next/prev/ungraded),
    and submission page images. Use this before applying grades.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        submission_id: The question submission ID.
    """
    return get_submission_grading_context(course_id, question_id, submission_id)


@mcp.tool()
def tool_apply_grade(
    course_id: str,
    question_id: str,
    submission_id: str,
    rubric_item_ids: list[str] | None = None,
    point_adjustment: float | None = None,
    comment: str | None = None,
    confidence: float | None = None,
    confirm_write: bool = False,
) -> str:
    """Apply a grade to a student's question submission.

    Can apply/remove rubric items, set point adjustments, and add comments.
    **WARNING**: This modifies student grades.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        submission_id: The question submission ID.
        rubric_item_ids: List of rubric item IDs to apply (checked). Items NOT
            in this list will be unchecked. ``None`` keeps current rubric
            state; ``[]`` clears all applied items.
        point_adjustment: Submission-specific point adjustment. Pass None to keep.
        comment: Per-submission comment (Gradescope's "Provide comments
            specific to this submission" field). ``None`` keeps current,
            ``""`` clears, any other string overwrites. Stored separately
            from rubric items.
        confidence: Agent's self-assessed grading confidence (0.0-1.0).
            Below 0.6 = rejected. 0.6-0.8 = warning. Above 0.8 = OK.
            Pass None to skip confidence gating (manual mode).
        confirm_write: Must be True to save the grade.
    """
    return apply_grade(
        course_id,
        question_id,
        submission_id,
        rubric_item_ids,
        point_adjustment,
        comment,
        confidence,
        confirm_write,
    )


@mcp.tool()
def tool_apply_grade_batch(
    course_id: str,
    question_id: str,
    grades: list[dict],
    confirm_write: bool = False,
) -> str:
    """Apply grades to many submissions for one question in a single call.

    Use this after the user approves a batch of previews. Subagents cannot call
    write-gated tools in the Claude Code harness, so all writes funnel through
    the main agent — batching cuts round-trips for large grading runs.

    Each entry in ``grades`` is a dict:

    - ``submission_id``: str (required)
    - ``rubric_item_ids``: list[str] | None (same semantics as ``apply_grade``;
      ``None`` keeps current rubric state, ``[]`` clears all items)
    - ``point_adjustment``: float | None (``None`` keeps current)
    - ``comment``: str | None (``None`` keeps current)
    - ``confidence``: float | None (per-row gate; < 0.6 is skipped)

    Behavior:
    - ``confirm_write=False``: returns a compact preview table; no writes.
    - ``confirm_write=True``: executes per-row; returns succeeded / failed /
      skipped-by-confidence counts plus per-row details.

    After execution, the agent is still expected to spot-check a submission
    via ``tool_get_submission_grading_context`` to confirm live state.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID that every entry in ``grades`` targets.
        grades: List of per-submission grade entries (see above).
        confirm_write: Must be True to save. Default False returns a preview.
    """
    return apply_grade_batch(course_id, question_id, grades, confirm_write)


@mcp.tool()
def tool_get_question_rubric(
    course_id: str,
    question_id: str,
) -> str:
    """Get rubric items for a question without needing a submission ID.

    Auto-discovers a submission to extract rubric data. Use when you know
    the question_id from outline but don't have a submission ID yet.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID from outline.
    """
    return get_question_rubric(course_id, question_id)


@mcp.tool()
def tool_create_rubric_item(
    course_id: str,
    question_id: str,
    description: str,
    weight: float,
    confirm_write: bool = False,
) -> str:
    """Create a new rubric item for a question.

    **WARNING**: Changes the rubric for ALL submissions.

    Weight is always a **positive** number. The question's ``scoring_type``
    determines interpretation:
    - **Positive scoring:** weight = points earned.
    - **Negative scoring:** weight = points deducted (e.g., ``2.0`` → −2 on web UI).

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        description: Rubric item description.
        weight: Point value — always positive.
        confirm_write: Must be True to create the rubric item.
    """
    return create_rubric_item(
        course_id, question_id, description, weight, confirm_write
    )


@mcp.tool()
def tool_list_question_submissions(
    course_id: str, question_id: str, filter: str = "all",
) -> str:
    """List all Question Submission IDs for a question.

    Use this to pre-allocate submission IDs to subagents for parallel
    grading. Returns **Question Submission IDs** (not Global IDs) that
    work directly with ``get_submission_grading_context`` and ``apply_grade``.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        filter: "all" (default), "ungraded", or "graded".
    """
    return list_question_submissions(course_id, question_id, filter)


@mcp.tool()
def tool_get_student_submission_map(
    course_id: str,
    assignment_id: str,
    student_name: str = "",
) -> str:
    """Build a per-student → {question_id: submission_id} map.

    Replaces "fetch each question's submissions list, then join by student"
    boilerplate. Especially useful when reviewing several questions for one
    student (e.g. spot-checking a low-prediction-accuracy outlier across
    Q1-Q5).

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_name: Optional exact-match filter (case-sensitive,
            "First Last" as shown on Gradescope's submissions page).

    Returns JSON with ``questions`` and ``students`` lists. Submission IDs
    are Question Submission IDs ready for grading tools.
    """
    return get_student_submission_map(course_id, assignment_id, student_name)


@mcp.tool()
def tool_get_next_ungraded(
    course_id: str, question_id: str, submission_id: str = "",
    output_format: str = "markdown",
) -> str:
    """Navigate to the next ungraded submission.

    Returns the full grading context for the next ungraded submission,
    or a message that all submissions are graded.

    Args:
        course_id: The current course ID.
        question_id: The current question ID.
        submission_id: The current Question Submission ID (optional).
            If omitted or invalid (e.g. a Global Submission ID), the tool
            will auto-discover a valid submission to navigate from.
        output_format: "markdown" (default) or "json".
    """
    return get_next_ungraded(course_id, question_id, submission_id, output_format)


@mcp.tool()
def tool_update_rubric_item(
    course_id: str,
    question_id: str,
    rubric_item_id: str,
    description: str | None = None,
    weight: float | None = None,
    confirm_write: bool = False,
) -> str:
    """Update an existing rubric item's description or weight.

    **WARNING**: Changes cascade to ALL submissions with this item applied.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        rubric_item_id: The rubric item ID to update.
        description: New description, or None to keep unchanged.
        weight: New point value, or None to keep unchanged.
        confirm_write: Must be True to apply the update.
    """
    return update_rubric_item(
        course_id, question_id, rubric_item_id, description, weight, confirm_write
    )


@mcp.tool()
def tool_delete_rubric_item(
    course_id: str,
    question_id: str,
    rubric_item_id: str,
    confirm_write: bool = False,
) -> str:
    """Delete a rubric item from a question.

    **WARNING**: Removes the item from ALL submissions and recalculates scores.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        rubric_item_id: The rubric item ID to delete.
        confirm_write: Must be True to delete the item.
    """
    return delete_rubric_item(
        course_id, question_id, rubric_item_id, confirm_write
    )


@mcp.tool()
def tool_get_answer_groups(
    course_id: str,
    question_id: str,
    output_format: str = "markdown",
) -> str:
    """List all answer groups for a question (AI-Assisted Grading).

    Shows clusters of similar student answers. Grade one group to
    grade all members at once — much more efficient than 1-by-1.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        output_format: "markdown" or "json" for structured output.
    """
    return get_answer_groups(course_id, question_id, output_format)


@mcp.tool()
def tool_get_answer_group_detail(
    course_id: str,
    question_id: str,
    group_id: str,
    output_format: str = "markdown",
) -> str:
    """Get detail for one answer group: members, crops, graded status.

    Use this to inspect what answers are in a group before batch-grading.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        group_id: The answer group ID.
        output_format: "markdown" or "json" for structured output.
    """
    return get_answer_group_detail(course_id, question_id, group_id, output_format)


@mcp.tool()
def tool_grade_answer_group(
    course_id: str,
    question_id: str,
    group_id: str,
    rubric_item_ids: list[str] | None = None,
    point_adjustment: float | None = None,
    comment: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Batch-grade ALL submissions in an answer group at once.

    **WARNING**: This grades N students at once. Use with caution.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID.
        group_id: The answer group ID.
        rubric_item_ids: Rubric item IDs to apply.
        point_adjustment: Point adjustment.
        comment: Grader comment.
        confirm_write: Must be True to apply grades.
    """
    return grade_answer_group(
        course_id, question_id, group_id,
        rubric_item_ids, point_adjustment, comment, confirm_write
    )


@mcp.tool()
def tool_prepare_grading_artifact(
    course_id: str,
    assignment_id: str | None = None,
    question_id: str = "",
    submission_id: str | None = None,
) -> str:
    """Prepare a cached `/tmp/gradescope-mcp` markdown artifact for grading a question.

    The artifact includes the prompt, rubric, reference-answer notes, page URLs,
    crop regions, and a confidence gate for auto-grading.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: Optional assignment ID. If omitted or wrong, the tool
            will try to resolve the owning assignment from question_id.
        question_id: The question ID to prepare.
        submission_id: Optional sample submission ID to use for rubric/page context.
    """
    return prepare_grading_artifact(
        course_id, assignment_id, question_id, submission_id
    )


@mcp.tool()
def tool_assess_submission_readiness(
    course_id: str,
    assignment_id: str | None = None,
    question_id: str = "",
    submission_id: str = "",
) -> str:
    """Assess whether an agent should auto-grade a specific submission.

    Returns a crop-first read plan, fallback rules for whole-page/adjacent-page
    reads, and a coarse confidence score that can be used to skip or escalate.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: Optional assignment ID. If omitted or wrong, the tool
            will try to resolve the owning assignment from question_id.
        question_id: The question ID.
        submission_id: The question submission ID.
    """
    return assess_submission_readiness(
        course_id, assignment_id, question_id, submission_id
    )


@mcp.tool()
def tool_cache_relevant_pages(
    course_id: str,
    assignment_id: str | None = None,
    question_id: str = "",
    submission_id: str = "",
    include_all_pages: bool = True,
) -> str:
    """Download the crop page and neighboring pages to `/tmp/gradescope-mcp` for local review.

    This is useful for scanned exams where the prompt is only available in page
    images and where agents may need to inspect adjacent pages before grading.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: Optional assignment ID. If omitted or wrong, the tool
            will try to resolve the owning assignment from question_id.
        question_id: The question ID.
        submission_id: The question submission ID.
        include_all_pages: Default ``True`` caches every page of the
            submission. This is the safe default for scanned exams because
            students routinely tag the wrong page for a question, and the
            extra pages are cheap. Set ``False`` to cache only the crop
            page and its immediate neighbors (driven by the student's
            tagging) when you've already verified tagging is reliable
            for this assignment.
    """
    return cache_relevant_pages(
        course_id, assignment_id, question_id, submission_id, include_all_pages
    )


@mcp.tool()
def tool_prepare_answer_key(course_id: str, assignment_id: str) -> str:
    """Prepare a complete answer key for an entire assignment.

    Extracts ALL questions from the outline (prompt text, reference answers,
    explanations) and saves to
    `/tmp/gradescope-mcp/gradescope-answerkey-{assignment_id}.md`.
    Run this ONCE before grading to avoid re-fetching question details.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    return prepare_answer_key(course_id, assignment_id)


@mcp.tool()
def tool_smart_read_submission(
    course_id: str,
    assignment_id: str | None = None,
    question_id: str = "",
    submission_id: str = "",
) -> str:
    """Get a smart, tiered reading plan for a student's submission.

    Returns page URLs in priority order:
    - Tier 1: Crop region only (read FIRST)
    - Tier 2: Full page (if answer overflows crop)
    - Tier 3: Adjacent pages (if still incomplete)

    Also returns confidence score and recommended action.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: Optional assignment ID. If omitted or wrong, the tool
            will try to resolve the owning assignment from question_id.
        question_id: The question ID.
        submission_id: The question submission ID.
    """
    return smart_read_submission(
        course_id, assignment_id, question_id, submission_id
    )


# ============================================================
# Resources
# ============================================================


@mcp.resource("gradescope://courses")
def resource_courses() -> str:
    """Current list of all Gradescope courses for the authenticated user."""
    return list_courses()


@mcp.resource("gradescope://courses/{course_id}/assignments")
def resource_assignments(course_id: str) -> str:
    """List of assignments for a specific course."""
    return get_assignments(course_id)


@mcp.resource("gradescope://courses/{course_id}/roster")
def resource_roster(course_id: str) -> str:
    """Course roster for a specific course."""
    return get_course_roster(course_id)


# ============================================================
# Prompts
# ============================================================


@mcp.prompt()
def summarize_course_progress(course_id: str) -> str:
    """Generate a summary of all assignment progress for a course.

    Useful for getting a quick overview of upcoming deadlines,
    submission status, and grades.
    """
    return (
        f"Please analyze the assignments for Gradescope course {course_id}. "
        f"First, call tool_get_assignments with course_id='{course_id}' "
        f"to get the full assignment list. Then provide:\n"
        f"1. A summary of all assignments and their current status\n"
        f"2. Upcoming deadlines (sorted by date)\n"
        f"3. Any assignments that are past due but not yet submitted\n"
        f"4. Overall grade summary if available\n"
        f"Format the response in a clear, organized manner."
    )


@mcp.prompt()
def manage_extensions_workflow(course_id: str, assignment_id: str) -> str:
    """Walk through the process of managing extensions for an assignment.

    Guides the user through viewing current extensions and adding new ones.
    """
    return (
        f"Help me manage extensions for assignment {assignment_id} in course {course_id}. "
        f"Please:\n"
        f"1. First, call tool_get_extensions with course_id='{course_id}' and "
        f"assignment_id='{assignment_id}' to see current extensions\n"
        f"2. Call tool_get_course_roster with course_id='{course_id}' to get student list with user IDs\n"
        f"3. Show me the current extensions and the roster, then ask which students "
        f"need extensions and what dates to set\n"
        f"4. Use tool_set_extension to apply the requested changes"
    )


@mcp.prompt()
def check_submission_stats(course_id: str, assignment_id: str) -> str:
    """Check submission statistics for an assignment.

    Provides an overview of how many students have submitted.
    """
    return (
        f"Please check the submission statistics for assignment {assignment_id} "
        f"in course {course_id}. Steps:\n"
        f"1. Call tool_get_course_roster with course_id='{course_id}' to get the full roster\n"
        f"2. Call tool_get_assignment_details with course_id='{course_id}' and "
        f"assignment_id='{assignment_id}' for assignment info\n"
        f"3. Provide a summary including:\n"
        f"   - Total enrolled students\n"
        f"   - Assignment due date\n"
        f"   - Any relevant observations about the assignment status"
    )


@mcp.prompt()
def generate_rubric_from_outline(course_id: str, assignment_id: str) -> str:
    """Generate rubric suggestions for an assignment based on its question outline.

    Analyzes the assignment structure and proposes rubric items for each question.
    """
    return (
        f"I need help creating a grading rubric for assignment {assignment_id} in course {course_id}.\n\n"
        f"Please follow these steps:\n"
        f"1. Call tool_get_assignment_outline with course_id='{course_id}' and "
        f"assignment_id='{assignment_id}' to get the full question structure.\n"
        f"2. For EACH question, create a rubric with:\n"
        f"   - Full credit criteria (what earns the full weight)\n"
        f"   - Partial credit levels (e.g., 75%, 50%, 25% of weight)\n"
        f"   - Common deduction items (missing explanation, wrong method, etc.)\n"
        f"   - Zero credit criteria\n"
        f"3. If the question has an answer key/explanation, use it to inform the rubric\n"
        f"4. Present the rubric as a structured table for each question group\n"
        f"5. Ask me to review and adjust before finalizing"
    )


@mcp.prompt()
def grade_submission_with_rubric(
    course_id: str, assignment_id: str, student_email: str
) -> str:
    """Grade a student's submission using the assignment rubric.

    Reads the assignment outline, fetches the student's submission,
    and produces a detailed grading report.
    """
    return (
        f"Please grade the submission from {student_email} for assignment {assignment_id} "
        f"in course {course_id}.\n\n"
        f"Follow these steps:\n"
        f"1. Call tool_get_assignment_outline with course_id='{course_id}' and "
        f"assignment_id='{assignment_id}' to understand the question structure and weights\n"
        f"2. Call tool_get_student_submission with course_id='{course_id}', "
        f"assignment_id='{assignment_id}', and student_email='{student_email}' to get their files\n"
        f"3. Analyze each submitted answer against the question requirements\n"
        f"4. For each question, provide:\n"
        f"   - Score (out of the question weight)\n"
        f"   - Justification for the score\n"
        f"   - Specific feedback for the student\n"
        f"5. Calculate the total score\n"
        f"6. Present in a clear grading report format\n"
        f"7. Ask me to confirm before any scores are applied"
    )


@mcp.prompt()
def review_regrade_requests(
    course_id: str, assignment_id: str
) -> str:
    """Review all pending regrade requests for an assignment.

    AI reviews each student's regrade argument against the rubric
    and original grading, then suggests accept/reject with reasoning.
    """
    return (
        f"Please review all pending regrade requests for assignment {assignment_id} "
        f"in course {course_id}.\n\n"
        f"Follow these steps:\n"
        f"1. Call tool_get_regrade_requests with course_id='{course_id}' and "
        f"assignment_id='{assignment_id}' to list all requests\n"
        f"2. Call tool_get_assignment_outline with the same IDs to understand the rubric\n"
        f"3. For each PENDING request, call tool_get_regrade_detail with the "
        f"question_id and submission_id to see the student's message and applied rubric\n"
        f"4. For each request, provide:\n"
        f"   - Student name and question\n"
        f"   - Summary of the student's argument\n"
        f"   - Your assessment: is the argument valid?\n"
        f"   - Recommendation: ACCEPT (adjust grade) or REJECT (keep current grade)\n"
        f"   - Suggested response to the student\n"
        f"5. Present all reviews in a summary table\n"
        f"6. Ask me to confirm before any changes are made"
    )


@mcp.prompt()
def auto_grade_question(
    course_id: str, assignment_id: str, question_id: str
) -> str:
    """Smart auto-grading workflow for a single question.

    Guides the agent through the complete grading pipeline:
    1. Prepare answer key (once per assignment)
    2. For each submission: smart read → assess → grade → navigate next
    3. Uses confidence gating to skip uncertain submissions
    """
    return (
        f"Auto-grade question {question_id} for assignment {assignment_id} "
        f"in course {course_id}.\n\n"
        f"Follow this workflow:\n\n"
        f"**Step 1 — Prepare Answer Key (one-time)**\n"
        f"Call tool_prepare_answer_key(course_id='{course_id}', "
        f"assignment_id='{assignment_id}'). Read the generated "
        f"/tmp/gradescope-mcp file to "
        f"understand all questions and reference answers.\n\n"
        f"**Step 2 — Get Grading Context**\n"
        f"Call tool_prepare_grading_artifact(course_id='{course_id}', "
        f"assignment_id='{assignment_id}', question_id='{question_id}') "
        f"to get rubric items, crop regions, and readiness score.\n\n"
        f"**Step 3 — For Each Submission (loop)**\n"
        f"a) Call tool_smart_read_submission to get the tiered reading plan.\n"
        f"b) Read **Tier 1 (crop only)** first. If the answer is complete, proceed.\n"
        f"   If truncated, escalate to Tier 2 (full page), then Tier 3 (adjacent).\n"
        f"c) After reading the student's work, self-assess your **grading confidence**:\n"
        f"   - How clear is the student's handwriting/answer?\n"
        f"   - How certain are you about which rubric items apply?\n"
        f"   - Are there any ambiguities you cannot resolve?\n"
        f"   - Assign a confidence score from 0.0 to 1.0.\n"
        f"d) Apply grade via tool_apply_grade with:\n"
        f"   - rubric_item_ids, comment, optional point_adjustment\n"
        f"   - **confidence=YOUR_SCORE** (this gates the write)\n"
        f"   - confirm_write=True\n"
        f"e) Call tool_get_next_ungraded to move to the next submission.\n\n"
        f"**Confidence Thresholds:**\n"
        f"- `confidence >= 0.8`: Grade is saved normally.\n"
        f"- `confidence 0.6-0.8`: Grade is saved with a warning for human review.\n"
        f"- `confidence < 0.6`: Grade is REJECTED. Skip this submission.\n\n"
        f"**Important Rules:**\n"
        f"- Never grade without reading the student's actual work first.\n"
        f"- Always self-report an honest confidence score.\n"
        f"- Always include a brief justification in the comment field.\n"
        f"- Present a summary after each batch of 5-10 submissions."
    )
