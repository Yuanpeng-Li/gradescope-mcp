"""Export an assignment's Gradescope scores as an LMS gradebook import CSV.

Builds on the assignment's ``/scores`` CSV export (one row per roster
student) and writes a file the LMS's gradebook import accepts:

- **Canvas**: ``Student, ID, SIS User ID, SIS Login ID, Section, <column>``
  plus a ``Points Possible`` row. Canvas's own ``ID`` is not known to
  Gradescope and is left blank; the Gradescope SID goes in ``SIS User ID``
  and the email in ``SIS Login ID``, which Canvas can match when they are the
  institution's SIS identifiers.
- **Brightspace**: an ``OrgDefinedId`` (Gradescope SID) or ``Username``
  (email local part) key column, ``<column> Points Grade`` (with a
  ``<Numeric MaxPoints:N>`` note so a new grade item gets the right maximum),
  and the required final ``End-of-Line Indicator`` column of ``#``. Keys are
  ``#``-prefixed like Brightspace's own exports.

Only fully graded submissions get a score by default. Missing students are
left blank (or 0 with ``missing="zero"``), and partially graded submissions
are left blank unless ``include_ungraded`` is set, so an unfinished grade is
never pushed into the gradebook by accident. The file is written to the
private runtime cache; the LMS's import preview remains the place to check
which students matched.
"""

from __future__ import annotations

import csv
import io

from gradescope_mcp.auth import AuthError, get_connection
from gradescope_mcp.cache import CacheError, get_artifact_path, write_artifact
from gradescope_mcp.tools.grading import _fetch_assignment_scores_csv, _parse_score

LMS_CHOICES = ("canvas", "brightspace")
MISSING_CHOICES = ("blank", "zero")
BRIGHTSPACE_KEYS = ("orgdefinedid", "username")

# Canvas ignores these as assignment columns (they identify the student).
_CANVAS_RESERVED = frozenset({
    "student", "id", "sis user id", "sis login id", "section",
    "integration id", "root account",
})
# A spreadsheet treats a cell starting with one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _format_points(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _safe_text(value: str) -> str:
    """Neutralize a student-controlled cell a spreadsheet would run as a formula."""
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


def _assignment_title(course_id: str, assignment_id: str) -> str | None:
    try:
        for assignment in get_connection().account.get_assignments(course_id):
            if str(assignment.assignment_id) == str(assignment_id):
                return (assignment.name or "").strip() or None
    except Exception:
        return None
    return None


def _uniform_max_points(rows: list[dict]) -> float | None:
    values = {_parse_score(r.get("Max Points")) for r in rows}
    values.discard(None)
    return values.pop() if len(values) == 1 else None


def _score_cell(row: dict, missing: str, include_ungraded: bool) -> tuple[str, str]:
    """(cell value, category) for one roster row."""
    status = (row.get("Status") or "").strip().lower()
    total = _parse_score(row.get("Total Score"))
    if status == "missing":
        return ("0" if missing == "zero" else ""), "missing"
    if status == "graded" and total is not None:
        return _format_points(total), "graded"
    if include_ungraded and total is not None:
        return _format_points(total), "partial"
    return "", "ungraded"


def export_lms_gradebook(
    course_id: str,
    assignment_id: str,
    lms: str = "canvas",
    column_name: str | None = None,
    missing: str = "blank",
    include_ungraded: bool = False,
    brightspace_key: str = "orgdefinedid",
    include_csv: bool = False,
) -> str:
    """Write an LMS gradebook import CSV for one assignment's scores.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        lms: ``"canvas"`` or ``"brightspace"``.
        column_name: Gradebook column / grade item name. Defaults to the
            Gradescope assignment title. To update an existing column, use
            its exact LMS name (Canvas: including the ``(id)`` suffix of an
            exported gradebook).
        missing: ``"blank"`` (default) leaves students without a submission
            empty; ``"zero"`` gives them 0.
        include_ungraded: Export the current (partial) total of submissions
            that are not fully graded instead of leaving them blank.
        brightspace_key: ``"orgdefinedid"`` (Gradescope SID) or
            ``"username"`` (the email's local part).
        include_csv: Also return the CSV text, not just the file path.
    """
    if not course_id or not assignment_id:
        return "Error: course_id and assignment_id are required."
    lms = (lms or "").strip().lower()
    if lms not in LMS_CHOICES:
        return f"Error: lms must be one of {', '.join(LMS_CHOICES)}."
    if missing not in MISSING_CHOICES:
        return f"Error: missing must be one of {', '.join(MISSING_CHOICES)}."
    if brightspace_key not in BRIGHTSPACE_KEYS:
        return f"Error: brightspace_key must be one of {', '.join(BRIGHTSPACE_KEYS)}."
    if column_name is not None:
        column_name = " ".join(column_name.split())
        if not column_name:
            return "Error: column_name cannot be blank."
        if lms == "canvas" and column_name.lower() in _CANVAS_RESERVED:
            return (
                f"Error: '{column_name}' is a reserved Canvas column name; "
                "choose the assignment's name."
            )

    try:
        rows, _fields = _fetch_assignment_scores_csv(course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    if not rows:
        return f"Error: no students in the scores export of assignment `{assignment_id}`."

    title_note = ""
    if column_name is None:
        title = _assignment_title(course_id, assignment_id)
        column_name = title or f"Gradescope {assignment_id}"
        if title is None:
            title_note = (
                " (the assignment title could not be read; pass column_name "
                "to use the LMS's name)"
            )
    max_points = _uniform_max_points(rows)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    counts = {"graded": 0, "missing": 0, "ungraded": 0, "partial": 0}
    skipped: list[str] = []

    if lms == "canvas":
        writer.writerow(["Student", "ID", "SIS User ID", "SIS Login ID", "Section", column_name])
        writer.writerow(
            ["Points Possible", "", "", "", "", _format_points(max_points) if max_points is not None else ""]
        )
        for row in rows:
            score, category = _score_cell(row, missing, include_ungraded)
            counts[category] += 1
            last = (row.get("Last Name") or "").strip()
            first = (row.get("First Name") or "").strip()
            name = f"{last}, {first}" if last and first else (last or first)
            writer.writerow([
                _safe_text(name),
                "",
                (row.get("SID") or "").strip(),
                (row.get("Email") or "").strip(),
                _safe_text((row.get("Sections") or "").strip()),
                score,
            ])
        key_label = "SIS User ID = Gradescope SID, SIS Login ID = email"
    else:
        key_header = "OrgDefinedId" if brightspace_key == "orgdefinedid" else "Username"
        grade_header = f"{column_name} Points Grade"
        if max_points is not None:
            grade_header += f" <Numeric MaxPoints:{_format_points(max_points)}>"
        writer.writerow([key_header, grade_header, "End-of-Line Indicator"])
        for row in rows:
            if brightspace_key == "orgdefinedid":
                key = (row.get("SID") or "").strip()
            else:
                key = (row.get("Email") or "").strip().split("@", 1)[0]
            if not key:
                name = " ".join(
                    part for part in ((row.get("First Name") or "").strip(),
                                      (row.get("Last Name") or "").strip()) if part
                )
                skipped.append(name or (row.get("Email") or "").strip() or "(unnamed)")
                continue
            score, category = _score_cell(row, missing, include_ungraded)
            counts[category] += 1
            writer.writerow([f"#{key}", score, "#"])
        key_label = (
            "OrgDefinedId = Gradescope SID" if brightspace_key == "orgdefinedid"
            else "Username = email local part"
        )

    text = buffer.getvalue()
    try:
        path = write_artifact(get_artifact_path(f"lms-{lms}-{course_id}-{assignment_id}.csv"), text)
    except (CacheError, OSError) as e:
        return f"Error: could not write the CSV to the runtime cache: {e}"

    exported = counts["graded"] + counts["partial"] + (counts["missing"] if missing == "zero" else 0)
    lines = [
        f"✅ {'Canvas' if lms == 'canvas' else 'Brightspace'} gradebook CSV written for "
        f"assignment `{assignment_id}`.",
        f"- File: `{path}`",
        f"- Column: \"{column_name}\"{title_note}"
        + (f"; points possible {_format_points(max_points)}" if max_points is not None else
           "; points possible not set (Max Points differs between students)"),
        f"- Students matched by: {key_label}",
        f"- Rows: {counts['graded'] + counts['partial'] + counts['missing'] + counts['ungraded']}"
        f" ({exported} with a score)",
        f"- Fully graded: {counts['graded']}",
        f"- Missing: {counts['missing']} ({'0 points' if missing == 'zero' else 'left blank'})",
    ]
    if include_ungraded:
        lines.append(f"- Not fully graded, exported with their current partial total: {counts['partial']}")
    else:
        lines.append(f"- Not fully graded, left blank: {counts['ungraded']}")
    if skipped:
        shown = ", ".join(skipped[:5]) + (f", and {len(skipped) - 5} more" if len(skipped) > 5 else "")
        lines.append(
            f"- ⚠️ Left out (no {'SID' if brightspace_key == 'orgdefinedid' else 'email'} to "
            f"match on): {len(skipped)} — {shown}"
        )
    if lms == "canvas":
        lines.append(
            "- Import in Canvas: Grades → Import. The ID column is blank, so Canvas has to "
            "match students by the SIS columns; check the import preview for unmatched "
            "students before applying. A column name that matches no assignment creates a "
            "new assignment (not possible when multiple grading periods are enabled)."
        )
    else:
        lines.append(
            "- Import in Brightspace: Grades → Enter Grades → Import. The grade item name "
            "must match an existing item unless you let the import create it; check the "
            "preview for unmatched students before saving."
        )
    if include_csv:
        lines += ["", "```csv", text.replace("\r\n", "\n").rstrip("\n"), "```"]
    return "\n".join(lines)
