"""structuredContent for JSON results, advisory markup warnings on grade and
rubric writes, and the score check behind a verified grade write."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client

from gradescope_mcp import auth, server
from gradescope_mcp.tools import grading_ops

from test_grading_ops_fixes import C, Q, FakeGradescope, _sub

DOUBLE_WRAPPED = "Use $$$$x^2$$$$ here"


# ---------------------------------------------------------------------------
# structured_json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"a": 1, "b": [1, 2]}', {"a": 1, "b": [1, 2]}),
        ('  \n{"a": "ü"}\n', {"a": "ü"}),
        ("[1, 2]", None),
        ('"just a string"', None),
        ("## Heading\n{not json}", None),
        ("{not json}", None),
        ('{"score": NaN}', None),
        ('{"score": Infinity}', None),
        ('{"a": 1}\n\n⚠️ The Gradescope session expired ...', None),
        ('{"a": "\\ud800"}', None),  # lone surrogate: not encodable
        ('{"a": "\\u00fc\\ud83d\\ude00"}', {"a": "ü😀"}),
        ("", None),
    ],
)
def test_structured_json_only_accepts_a_whole_json_object(text, expected) -> None:
    assert server.structured_json(text) == expected


def test_structured_json_skips_oversized_text(monkeypatch) -> None:
    monkeypatch.setattr(server, "MAX_STRUCTURED_TEXT", 20)

    assert server.structured_json('{"a": "' + "x" * 30 + '"}') is None
    assert server.structured_json('{"a": 1}') == {"a": 1}


# ---------------------------------------------------------------------------
# Through the MCP layer
# ---------------------------------------------------------------------------


def _client_calls(calls):
    async def session():
        async with Client(server.mcp) as client:
            listed = await client.list_tools()
            results = [await client.call_tool(name, args) for name, args in calls]
            return listed, results

    return anyio.run(session)


def test_json_result_carries_structured_content_over_the_protocol(monkeypatch) -> None:
    payload = {"operation_token": "op-1", "status": "completed", "rows": [{"n": 1}]}
    text = json.dumps(payload)
    monkeypatch.setattr(server, "get_operation", lambda *_a: text)
    monkeypatch.setattr(server, "list_courses", lambda: "## Courses\n- Math 1")

    listed, (json_result, md_result) = _client_calls([
        ("tool_get_operation", {"operation_token": "op-1"}),
        ("tool_list_courses", {}),
    ])

    tool = next(t for t in listed.tools if t.name == "tool_get_operation")
    assert tool.output_schema is None
    assert json_result.is_error is False
    assert json_result.structured_content == payload
    # Text clients see exactly the same text as before.
    assert [c.text for c in json_result.content] == [text]
    assert md_result.is_error is False
    assert md_result.structured_content is None
    assert [c.text for c in md_result.content] == ["## Courses\n- Math 1"]


def test_error_results_stay_text_only(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_operation", lambda *_a: "Error: Cannot read operation: x")

    def boom():
        raise auth.AuthError("login failed")

    monkeypatch.setattr(server, "list_courses", boom)

    _listed, (handled, auth_failed, invalid) = _client_calls([
        ("tool_get_operation", {"operation_token": "op-1"}),
        ("tool_list_courses", {}),
        ("tool_get_assignments", {"course_id": "../1"}),
    ])

    for result in (handled, auth_failed, invalid):
        assert result.is_error is True
        assert result.structured_content is None
    assert handled.content[0].text == "Error: Cannot read operation: x"
    assert auth_failed.content[0].text == "Authentication error: login failed"


def test_json_array_result_is_text_only(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_operation", lambda *_a: "[1, 2, 3]")

    result = anyio.run(server.mcp.call_tool, "tool_get_operation", {"operation_token": "t"})

    assert result.is_error is False
    assert result.structured_content is None
    assert result.content[0].text == "[1, 2, 3]"


_EXPIRED = object()


def test_json_with_session_expiry_notice_is_text_only() -> None:
    """A JSON result followed by the accepted-writes notice is not parsed, so
    a client reading structuredContent cannot miss the notice."""

    def tool() -> str:
        auth._local.expired = _EXPIRED
        auth._local.writes = 1
        return json.dumps({"status": "completed"})

    text = server._signal_errors(auth.with_session_recovery(tool))()

    # Returned as plain text, which the SDK sends as text content only (no
    # structuredContent, isError false).
    assert isinstance(text, str)
    assert text.startswith('{"status": "completed"}')
    assert text.endswith(auth._writes_then_expiry_notice(1))


def test_json_without_expiry_gets_structured_content() -> None:
    tool = server._signal_errors(auth.with_session_recovery(lambda: '{"ok": true}'))

    result = tool()

    assert result.structured_content == {"ok": True}
    assert result.content[0].text == '{"ok": true}'


# ---------------------------------------------------------------------------
# Advisory formatting warnings (never block)
# ---------------------------------------------------------------------------


def test_apply_grade_preview_and_result_warn_about_doubled_dollars(monkeypatch) -> None:
    world = FakeGradescope({"11": _sub()}).install(monkeypatch)
    world.score_after_save["11"] = 6.0

    preview = grading_ops.apply_grade(C, Q, "11", rubric_item_ids=["200"], comment=DOUBLE_WRAPPED)
    assert preview.startswith("Write confirmation required")
    assert "⚠️ Formatting warning (comment; advisory, the write is not blocked)" in preview
    assert "wrapped in '$$' twice" in preview
    assert world.writes() == []

    result = grading_ops.apply_grade(
        C, Q, "11", rubric_item_ids=["200"], comment=DOUBLE_WRAPPED, confirm_write=True
    )
    assert result.startswith("✅ Grade saved successfully!")
    assert "Formatting warning (comment" in result
    assert world.saved["11"]["question_submission_evaluation"]["comments"] == DOUBLE_WRAPPED


def test_clean_comment_has_no_formatting_warning(monkeypatch) -> None:
    FakeGradescope({"11": _sub()}).install(monkeypatch)

    preview = grading_ops.apply_grade(
        C, Q, "11", rubric_item_ids=["200"], comment="Good: $x^2$ and $$\\frac{1}{2}$$"
    )

    assert "Formatting warning" not in preview


def test_batch_preview_and_result_list_rows_with_formatting_warnings(monkeypatch) -> None:
    world = FakeGradescope({"21": _sub(), "22": _sub()}).install(monkeypatch)
    world.score_after_save.update({"21": 10.0, "22": 10.0})
    grades = [
        {"submission_id": "21", "rubric_item_ids": ["100"], "comment": DOUBLE_WRAPPED},
        {"submission_id": "22", "rubric_item_ids": ["100"], "comment": "fine $x$"},
    ]

    preview = grading_ops.apply_grade_batch(C, Q, grades)
    result = grading_ops.apply_grade_batch(C, Q, grades, confirm_write=True)

    assert "⚠️ Formatting warnings in 1 comment(s)" in preview
    assert "`21`: three or more consecutive '$'" in preview
    assert "`22`:" not in preview.split("Formatting warnings", 1)[1].split("\n")[0]
    assert "**succeeded:** 2" in result
    assert "### ⚠️ Formatting warnings (advisory)" in result
    assert set(world.saved) == {"21", "22"}


def test_rubric_create_and_update_warn_about_description_markup(monkeypatch) -> None:
    world = FakeGradescope({"11": _sub()}).install(monkeypatch)
    bad = "Missing $\\frac{1}{2$ term"

    create_preview = grading_ops.create_rubric_item(C, Q, bad, 2.0)
    update_preview = grading_ops.update_rubric_item(C, Q, "200", description=bad)
    update_result = grading_ops.update_rubric_item(
        C, Q, "200", description=bad, confirm_write=True
    )
    created = grading_ops.create_rubric_item(C, Q, bad, 2.0, confirm_write=True)

    for text in (create_preview, update_preview, update_result, created):
        assert "Formatting warning (description; advisory" in text
        assert "unbalanced '{' '}'" in text
    assert update_result.startswith("✅ Rubric item `200` updated!")
    assert created.startswith("✅ Rubric item created!")
    assert len(world.writes()) == 2


# ---------------------------------------------------------------------------
# Verified requires the read-back score to equal the projection
# ---------------------------------------------------------------------------


def test_batch_row_with_projected_score_read_back_is_verified(monkeypatch) -> None:
    world = FakeGradescope({"21": _sub()}).install(monkeypatch)
    world.score_after_save["21"] = 6.0 + 1e-9  # rounding noise

    result = grading_ops.apply_grade_batch(
        C, Q, [{"submission_id": "21", "rubric_item_ids": ["200"]}], confirm_write=True
    )

    assert "- **succeeded:** 1" in result
    assert "score match):** 1" in result
    assert "needs check" not in result.lower()


def test_batch_row_with_different_score_needs_check(monkeypatch) -> None:
    world = FakeGradescope({"21": _sub(), "22": _sub()}).install(monkeypatch)
    world.score_after_save.update({"21": 2.0, "22": 6.0})
    grades = [
        {"submission_id": "21", "rubric_item_ids": ["200"]},
        {"submission_id": "22", "rubric_item_ids": ["200"]},
    ]

    result = grading_ops.apply_grade_batch(C, Q, grades, confirm_write=True)

    assert "- **succeeded:** 2" in result
    assert "score match):** 1" in result
    assert "- **needs check (saved, read-back does not confirm it):** 1" in result
    assert "- `21`: score on Gradescope is 2, projected 6" in result
    assert "- `22`: score on" not in result


def test_batch_row_without_read_back_score_needs_check(monkeypatch) -> None:
    FakeGradescope({"21": _sub()}).install(monkeypatch)  # no score after save

    result = grading_ops.apply_grade_batch(
        C, Q, [{"submission_id": "21", "rubric_item_ids": ["200"]}], confirm_write=True
    )

    assert "score match):** 0" in result
    assert "- `21`: Gradescope's page showed no score after the save" in result


def test_batch_row_with_field_mismatch_needs_check(monkeypatch) -> None:
    world = FakeGradescope({"21": _sub()}).install(monkeypatch)
    world.score_after_save["21"] = 6.0
    real_props = world.props_for

    def drop_comment(sid):
        props = real_props(sid)
        if sid in world.saved:
            props["evaluation"]["comments"] = None
        return props

    world.props_for = drop_comment

    result = grading_ops.apply_grade_batch(
        C, Q, [{"submission_id": "21", "rubric_item_ids": ["200"], "comment": "note"}],
        confirm_write=True,
    )

    assert "- **needs check (saved, read-back does not confirm it):** 1" in result
    assert "- `21`: the comment on Gradescope differs from the comment sent" in result


def test_readback_problems_shape() -> None:
    plan = {"projected_score": 6.0}

    ok = {"score": "6.0", "graded": True, "mismatches": []}
    assert grading_ops._readback_problems(plan, ok) == []
    assert grading_ops._readback_problems(plan, {"error": "boom"}) == ["read-back failed: boom"]
    assert grading_ops._readback_problems(
        {"projected_score": None}, {**ok, "score": 6.0}
    ) == ["score 6 could not be compared: no projected score (question weight unknown)"]


@pytest.mark.parametrize("graded", [False, None, "true"])
def test_readback_problems_require_graded_flag(graded) -> None:
    """A matching score is not verified unless the page says graded=True."""
    readback = {"score": 6.0, "mismatches": []}
    if graded is not None:
        readback["graded"] = graded
    problems = grading_ops._readback_problems({"projected_score": 6.0}, readback)
    assert len(problems) == 1 and "not show the submission as graded" in problems[0]


def test_apply_grade_reports_verified_or_needs_check(monkeypatch) -> None:
    world = FakeGradescope({"11": _sub(), "12": _sub()}).install(monkeypatch)
    world.score_after_save.update({"11": 6.0, "12": 2.0})

    ok = grading_ops.apply_grade(C, Q, "11", rubric_item_ids=["200"], confirm_write=True)
    off = grading_ops.apply_grade(C, Q, "12", rubric_item_ids=["200"], confirm_write=True)

    assert "**Read-back:** verified" in ok
    assert "Needs check" not in ok
    assert "⚠️ **Needs check:** saved, but the read-back does not confirm it: score on Gradescope is 2, projected 6" in off
