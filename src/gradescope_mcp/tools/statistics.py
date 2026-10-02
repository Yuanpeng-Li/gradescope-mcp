"""Assignment statistics tools.

These tools provide read access to assignment-level and per-question statistics
from the Gradescope `/statistics.json` endpoint.
"""

import logging
import math
import re

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.common import escape_md_cell

logger = logging.getLogger(__name__)

_MISSING = "—"


def _num(value) -> float | None:
    """Parse a statistic as a float; None for null, '--', bools or junk."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(fraction: float | None) -> str:
    """Render a 0..1 fraction as a percentage, or '—' when unknown."""
    return _MISSING if fraction is None else f"{fraction * 100:.1f}%"


def _natural_key(title) -> list[tuple[int, object]]:
    """Sort key that orders '1.2' before '1.10' (numbers compared as numbers)."""
    parts = re.split(r"(\d+)", str(title))
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts if p]


def get_assignment_statistics(course_id: str, assignment_id: str) -> str:
    """Get comprehensive statistics for an assignment.

    Returns assignment-level summary (mean, median, min, max, std) and
    per-question breakdowns showing average scores, standard deviations,
    and number of graded submissions. Requires instructor/TA access.

    Missing or undefined statistics (null, '--') are shown as '—'.

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
            f"/assignments/{assignment_id}/statistics.json"
        )
        resp = conn.session.get(url)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching statistics: {e}"

    if resp.status_code != 200:
        return f"Error: Cannot access statistics (status {resp.status_code})."

    try:
        data = resp.json()
    except Exception:
        return (
            "Error: Failed to parse statistics response (expected JSON; the "
            "session may have expired)."
        )
    if not isinstance(data, dict):
        return "Error: Unexpected statistics response (not a JSON object)."

    info = data.get("assignment_statistics_info") or {}
    if not info or not isinstance(info, dict):
        return "No statistics data available for this assignment."

    # Assignment metadata
    assignment = info.get("assignment") or {}
    title = assignment.get("title", f"Assignment {assignment_id}")
    total_points = _num(assignment.get("totalPoints"))
    fully_graded = info.get("assignmentFullyGraded", False)

    lines = [f"## Statistics — {title}\n"]
    lines.append(
        f"**Total points:** {total_points if total_points is not None else _MISSING}"
    )
    lines.append(f"**Fully graded:** {'Yes' if fully_graded else 'No'}")
    if not fully_graded:
        lines.append(
            "_Grading is not complete: these statistics will change, and "
            "partially graded questions can look artificially low._"
        )

    summary_stats = info.get("summaryStatistics") or {}

    # Assignment-level summary
    summary = summary_stats.get("assignment") or {}
    if summary:
        def _with_points(fraction: float | None) -> str:
            text = _pct(fraction)
            if fraction is not None and total_points is not None:
                text += f" ({fraction * total_points:.1f}/{total_points})"
            return text

        lines.append("\n### Overall Performance")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Mean | {_with_points(_num(summary.get('mean')))} |")
        lines.append(f"| Median | {_with_points(_num(summary.get('median')))} |")
        lines.append(f"| Min | {_pct(_num(summary.get('min')))} |")
        lines.append(f"| Max | {_pct(_num(summary.get('max')))} |")
        lines.append(f"| Std Dev | {_pct(_num(summary.get('standardDeviation')))} |")

        reliability = summary.get("reliability")
        if reliability not in (None, "", "--"):
            lines.append(f"| Reliability | {escape_md_cell(reliability)} |")

    # Per-question statistics: prefer the detailed questions dict; fall back
    # to the simpler questionAverages list only when it is absent.
    q_stats = summary_stats.get("questions") or {}
    if isinstance(q_stats, list):
        q_stats = {str(i): q for i, q in enumerate(q_stats)}
    if not isinstance(q_stats, dict):
        q_stats = {}
    q_stats = {qid: qs for qid, qs in q_stats.items() if isinstance(qs, dict)}
    q_avgs = info.get("questionAverages") or []

    if q_stats:
        lines.append("\n### Per-Question Statistics\n")
        lines.append("| Question | Weight | Mean% | Graded | StdDev% |")
        lines.append("|----------|--------|-------|--------|---------|")

        sorted_qs = sorted(
            q_stats.items(),
            key=lambda x: _natural_key(x[1].get("title") or x[0]),
        )
        for qid, qs in sorted_qs:
            graded = qs.get("graded")
            lines.append(
                f"| {escape_md_cell(qs.get('title') or qid)} "
                f"| {escape_md_cell(qs.get('weight', '?'))} "
                f"| {_pct(_num(qs.get('mean')))} "
                f"| {escape_md_cell(graded) if graded is not None else _MISSING} "
                f"| {_pct(_num(qs.get('standardDeviation')))} |"
            )
    elif q_avgs:
        lines.append("\n### Per-Question Averages\n")
        lines.append("| Question | Weight | Mean% | Graded | Min% | Max% | StdDev% |")
        lines.append("|----------|--------|-------|--------|------|------|---------|")
        entries = [
            e for e in q_avgs if isinstance(e, (list, tuple)) and len(e) >= 2
        ]
        for q_label, q_avg_pct, *_rest in sorted(entries, key=lambda e: _natural_key(e[0])):
            # questionAverages already holds percentages, e.g. ["1.1", 90.0].
            avg = _num(q_avg_pct)
            avg_text = _MISSING if avg is None else f"{avg:.1f}%"
            lines.append(
                f"| {escape_md_cell(q_label)} | — | {avg_text} | — | — | — | — |"
            )

    # Identify struggling questions (mean < 70%). A question with nothing
    # graded yet reports a mean of 0 that says nothing about difficulty.
    struggling = []
    for qid, qs in q_stats.items():
        mean = _num(qs.get("mean"))
        if mean is None or _num(qs.get("graded")) == 0:
            continue
        if mean < 0.7:
            struggling.append((qs.get("title") or qid, mean * 100, qs.get("weight", 0)))

    if struggling:
        lines.append("\n### ⚠️ Low-Scoring Questions (< 70% avg)")
        if not fully_graded:
            lines.append("_Grading is still in progress; recheck once it is complete._")
        for q_title, mean, weight in sorted(struggling, key=lambda x: x[1]):
            lines.append(f"- **{q_title}** ({weight} pts): {mean:.1f}% average")

    return "\n".join(lines)
