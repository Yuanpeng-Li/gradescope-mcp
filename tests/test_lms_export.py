"""Issue #7: export an assignment's scores as an LMS gradebook import CSV."""

from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path

import anyio
import pytest

from gradescope_mcp import server
from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import lms_export

# Columns exactly as Gradescope's /scores export names them (observed live).
FIELDS = [
    "First Name", "Last Name", "SID", "Email", "Total Score", "Max Points",
    "Status", "Submission ID", "Submission Time", "Lateness (H:M:S)",
    "View Count", "Submission Count", "1: Chemistry (5.0 pts)", "2: Math (5.0 pts)",
]


def _row(first, last, sid, email, total, status, max_points="10.0", **extra):
    row = dict.fromkeys(FIELDS, "")
    row.update({
        "First Name": first, "Last Name": last, "SID": sid, "Email": email,
        "Total Score": total, "Max Points": max_points, "Status": status,
    })
    row.update(extra)
    return row


ROWS = [
    _row("Ada", "Lovelace", "1001", "ada@uni.edu", "9.5", "Graded"),
    _row("Alan", "Turing", "1002", "alan@uni.edu", "", "Missing"),
    _row("Grace", "Hopper", "1003", "grace@uni.edu", "4.0", "Ungraded"),
    _row("No", "Sid", "", "nosid@uni.edu", "10.0", "Graded"),
]


@pytest.fixture
def scores(monkeypatch):
    calls = []

    def fake_fetch(course_id, assignment_id):
        calls.append((course_id, assignment_id))
        return [dict(r) for r in ROWS], list(FIELDS)

    monkeypatch.setattr(lms_export, "_fetch_assignment_scores_csv", fake_fetch)
    monkeypatch.setattr(lms_export, "_assignment_title", lambda c, a: "Homework 3")
    return calls


def _written_csv(result: str) -> list[list[str]]:
    path = Path(re.search(r"File: `([^`]+)`", result).group(1))
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


def test_canvas_csv_has_the_import_columns_and_only_final_scores(scores) -> None:
    result = lms_export.export_lms_gradebook("1", "2")

    assert result.startswith("✅ Canvas gradebook CSV written for assignment `2`.")
    rows = _written_csv(result)
    assert rows[0] == ["Student", "ID", "SIS User ID", "SIS Login ID", "Section", "Homework 3"]
    assert rows[1] == ["Points Possible", "", "", "", "", "10"]
    assert rows[2] == ["Lovelace, Ada", "", "1001", "ada@uni.edu", "", "9.5"]
    assert rows[3] == ["Turing, Alan", "", "1002", "alan@uni.edu", "", ""]  # missing: blank
    assert rows[4] == ["Hopper, Grace", "", "1003", "grace@uni.edu", "", ""]  # partial: blank
    assert rows[5] == ["Sid, No", "", "", "nosid@uni.edu", "", "10"]
    assert "- Fully graded: 2" in result
    assert "- Missing: 1 (left blank)" in result
    assert "- Not fully graded, left blank: 1" in result
    assert "check the import preview" in result
    assert "```csv" not in result


def test_csv_file_is_private_and_in_the_runtime_cache(scores) -> None:
    result = lms_export.export_lms_gradebook("1", "2", "canvas")
    path = Path(re.search(r"File: `([^`]+)`", result).group(1))

    assert path.name == "lms-canvas-1-2.csv"
    assert path.parent == Path(os.environ["GRADESCOPE_MCP_CACHE_DIR"]).resolve() or \
        path.parent.resolve() == Path(os.environ["GRADESCOPE_MCP_CACHE_DIR"]).resolve()
    if hasattr(os, "getuid"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_missing_zero_and_partial_totals_are_opt_in(scores) -> None:
    result = lms_export.export_lms_gradebook(
        "1", "2", "canvas", missing="zero", include_ungraded=True
    )
    rows = _written_csv(result)

    assert [r[5] for r in rows[2:]] == ["9.5", "0", "4", "10"]
    assert "- Missing: 1 (0 points)" in result
    assert "- Not fully graded, exported with their current partial total: 1" in result


def test_brightspace_csv_uses_points_grade_and_end_of_line_columns(scores) -> None:
    result = lms_export.export_lms_gradebook("1", "2", "brightspace")
    rows = _written_csv(result)

    assert rows[0] == [
        "OrgDefinedId", "Homework 3 Points Grade <Numeric MaxPoints:10>", "End-of-Line Indicator",
    ]
    assert rows[1:] == [["#1001", "9.5", "#"], ["#1002", "", "#"], ["#1003", "", "#"]]
    # The student without a SID can't be matched and is reported, not guessed.
    assert "- ⚠️ Left out (no SID to match on): 1 — No Sid" in result
    assert "Students matched by: OrgDefinedId = Gradescope SID" in result


def test_brightspace_username_key_is_the_email_local_part(scores) -> None:
    result = lms_export.export_lms_gradebook("1", "2", "brightspace", brightspace_key="username")
    rows = _written_csv(result)

    assert rows[0][0] == "Username"
    assert [r[0] for r in rows[1:]] == ["#ada", "#alan", "#grace", "#nosid"]
    assert "Left out" not in result


def test_custom_column_name_and_varying_max_points(monkeypatch) -> None:
    rows = [_row("A", "B", "1", "a@x", "3", "Graded", max_points="5.0"),
            _row("C", "D", "2", "c@x", "8", "Graded", max_points="10.0")]
    monkeypatch.setattr(lms_export, "_fetch_assignment_scores_csv", lambda c, a: (rows, FIELDS))
    monkeypatch.setattr(lms_export, "_assignment_title", lambda c, a: "unused")

    result = lms_export.export_lms_gradebook("1", "2", "brightspace", column_name="  Quiz   1 ")
    written = _written_csv(result)

    assert written[0][1] == "Quiz 1 Points Grade"  # no MaxPoints note when it varies
    assert "points possible not set (Max Points differs between students)" in result


def test_title_falls_back_when_it_cannot_be_read(monkeypatch) -> None:
    monkeypatch.setattr(lms_export, "_fetch_assignment_scores_csv", lambda c, a: (list(ROWS), FIELDS))
    monkeypatch.setattr(lms_export, "_assignment_title", lambda c, a: None)

    result = lms_export.export_lms_gradebook("1", "2")

    assert _written_csv(result)[0][5] == "Gradescope 2"
    assert "the assignment title could not be read; pass column_name" in result


def test_spreadsheet_formulas_in_student_names_are_neutralized(monkeypatch) -> None:
    rows = [_row("=HYPERLINK(\"http://x\")", "@Evil", "7", "e@x", "1", "Graded")]
    monkeypatch.setattr(lms_export, "_fetch_assignment_scores_csv", lambda c, a: (rows, FIELDS))
    monkeypatch.setattr(lms_export, "_assignment_title", lambda c, a: "HW")

    written = _written_csv(lms_export.export_lms_gradebook("1", "2"))

    assert written[2][0] == "'@Evil, =HYPERLINK(\"http://x\")"


def test_include_csv_returns_the_text_too(scores) -> None:
    result = lms_export.export_lms_gradebook("1", "2", include_csv=True)

    assert "```csv\nStudent,ID,SIS User ID,SIS Login ID,Section,Homework 3\n" in result


@pytest.mark.parametrize("kwargs, message", [
    ({"lms": "moodle"}, "Error: lms must be one of canvas, brightspace."),
    ({"missing": "drop"}, "Error: missing must be one of blank, zero."),
    ({"brightspace_key": "sid"}, "Error: brightspace_key must be one of orgdefinedid, username."),
    ({"column_name": "   "}, "Error: column_name cannot be blank."),
    ({"column_name": "SIS User ID"}, "Error: 'SIS User ID' is a reserved Canvas column name"),
])
def test_invalid_arguments_are_refused_before_any_request(scores, kwargs, message) -> None:
    assert lms_export.export_lms_gradebook("1", "2", **kwargs).startswith(message)
    assert scores == []


def test_fetch_failures_are_reported(monkeypatch) -> None:
    def html_page(c, a):
        raise ValueError("Expected the scores CSV export but received an HTML page")

    monkeypatch.setattr(lms_export, "_fetch_assignment_scores_csv", html_page)
    assert lms_export.export_lms_gradebook("1", "2").startswith("Error: Expected the scores CSV")

    def no_login(c, a):
        raise AuthError("Missing Gradescope credentials.")

    monkeypatch.setattr(lms_export, "_fetch_assignment_scores_csv", no_login)
    assert lms_export.export_lms_gradebook("1", "2").startswith("Authentication error: Missing")

    monkeypatch.setattr(lms_export, "_fetch_assignment_scores_csv", lambda c, a: ([], FIELDS))
    assert lms_export.export_lms_gradebook("1", "2").startswith("Error: no students")


def test_mcp_tool_schema_and_annotations() -> None:
    tool = next(t for t in anyio.run(server.mcp.list_tools) if t.name == "tool_export_lms_gradebook")

    props = tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["course_id", "assignment_id"]
    assert props["lms"]["enum"] == ["canvas", "brightspace"]
    assert props["missing"]["enum"] == ["blank", "zero"]
    # Writes only a local cache file: not read-only, not destructive.
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False


def test_mcp_call_writes_the_file(scores) -> None:
    result = anyio.run(
        server.mcp.call_tool,
        "tool_export_lms_gradebook",
        {"course_id": 1, "assignment_id": "2", "lms": "brightspace"},
    )

    assert result.is_error is False
    assert "✅ Brightspace gradebook CSV written" in result.content[0].text
    assert scores == [("1", "2")]
