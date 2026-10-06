"""Tests that exercise the server through the mcp v2 ``MCPServer`` layer.

These pin the surface MCP clients actually see (registration counts,
annotations, argument validation, error signalling, result shape, prompt
text) and the v2 execution model: sync tool functions run on a worker
thread instead of blocking the event loop.
"""

from __future__ import annotations

import inspect
import threading

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError

from gradescope_mcp import server
from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import grading_ops
from gradescope_mcp.tools.grading_ops import (
    CONFIDENCE_REJECT_BELOW,
    CONFIDENCE_REVIEW_UP_TO,
    MAX_BATCH_ROWS,
)

ID_PARAMS = {
    "course_id", "assignment_id", "question_id", "submission_id", "group_id",
    "rubric_item_id", "user_id",
}

# Tools that write to Gradescope (all have confirm_write).
GRADESCOPE_WRITES = {
    "tool_upload_submission", "tool_upload_submission_for_student",
    "tool_set_extension", "tool_modify_assignment_dates",
    "tool_rename_assignment", "tool_update_autograder_image", "tool_apply_grade",
    "tool_apply_grade_batch", "tool_create_rubric_item", "tool_update_rubric_item",
    "tool_delete_rubric_item", "tool_grade_answer_group",
}
# Workflow tools that write only to the private local cache.
LOCAL_CACHE_WRITES = {
    "tool_prepare_grading_artifact", "tool_cache_relevant_pages", "tool_prepare_answer_key",
    "tool_export_lms_gradebook",
}
# Writes that create something new on every call. Everything else that writes
# is idempotent in the MCP sense (a repeat has no additional effect), even when
# a repeat returns a different result: a repeated tool_delete_rubric_item finds
# the item gone, a repeated tool_grade_answer_group without overwrite_graded is
# refused because the members are now graded.
NON_IDEMPOTENT = {
    "tool_upload_submission", "tool_upload_submission_for_student", "tool_create_rubric_item",
}


def _tools() -> dict:
    return {t.name: t for t in anyio.run(server.mcp.list_tools)}


def _call(name: str, args: dict):
    return anyio.run(server.mcp.call_tool, name, args)


def test_registers_expected_tools_resources_and_prompts() -> None:
    async def inventory():
        return (
            await server.mcp.list_tools(),
            await server.mcp.list_resources(),
            await server.mcp.list_resource_templates(),
            await server.mcp.list_prompts(),
        )

    tools, resources, templates, prompts = anyio.run(inventory)

    assert len(tools) == 41
    assert [r.uri for r in resources] == ["gradescope://courses"]
    assert sorted(t.uri_template for t in templates) == [
        "gradescope://courses/{course_id}/assignments",
        "gradescope://courses/{course_id}/roster",
    ]
    assert len(prompts) == 7


def test_sync_tool_runs_off_event_loop_thread(monkeypatch) -> None:
    seen = {}

    def fake_list_courses() -> str:
        seen["thread"] = threading.current_thread()
        return "courses"

    monkeypatch.setattr(server, "list_courses", fake_list_courses)

    async def call():
        seen["loop_thread"] = threading.current_thread()
        return await server.mcp.call_tool("tool_list_courses", {})

    result = anyio.run(call)

    assert result.is_error is False
    assert [c.text for c in result.content] == ["courses"]
    # Text only: the payload is not duplicated into structuredContent.
    assert result.structured_content is None
    assert seen["thread"] is not seen["loop_thread"]


def test_write_tool_preview_through_mcp_layer() -> None:
    result = _call(
        "tool_rename_assignment",
        {"course_id": "1", "assignment_id": "2", "new_title": "Midterm"},
    )

    assert result.is_error is False
    assert "No changes were made." in result.content[0].text


# ------------------------------------------------------------------
# Annotations and output schema
# ------------------------------------------------------------------


def test_every_tool_has_complete_annotations() -> None:
    tools = _tools()
    assert len(tools) == 41
    for name, tool in tools.items():
        ann = tool.annotations
        assert ann is not None, name
        assert ann.title and tool.title == ann.title, name
        for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
            assert getattr(ann, hint) is not None, (name, hint)
        assert ann.open_world_hint is True, name
        if ann.read_only_hint:
            assert ann.destructive_hint is False, name
            assert ann.idempotent_hint is True, name


def test_destructive_tools_are_exactly_the_confirm_write_tools() -> None:
    tools = _tools()
    destructive = {n for n, t in tools.items() if t.annotations.destructive_hint}
    with_confirm = {n for n, t in tools.items() if "confirm_write" in t.input_schema["properties"]}

    assert destructive == with_confirm == GRADESCOPE_WRITES
    for name in destructive:
        assert tools[name].annotations.read_only_hint is False, name


def test_read_only_and_local_cache_tool_sets() -> None:
    tools = _tools()
    read_only = {n for n, t in tools.items() if t.annotations.read_only_hint}
    local = {
        n for n, t in tools.items()
        if not t.annotations.read_only_hint and not t.annotations.destructive_hint
    }

    assert local == LOCAL_CACHE_WRITES
    assert read_only == set(tools) - GRADESCOPE_WRITES - LOCAL_CACHE_WRITES
    assert {"tool_assess_submission_readiness", "tool_smart_read_submission"} <= read_only
    non_idempotent = {n for n, t in tools.items() if not t.annotations.idempotent_hint}
    assert non_idempotent == NON_IDEMPOTENT


def test_tools_publish_no_output_schema() -> None:
    for name, tool in _tools().items():
        assert tool.output_schema is None, name


# ------------------------------------------------------------------
# ID validation and coercion
# ------------------------------------------------------------------


def _string_schemas(schema: dict) -> list[dict]:
    """The string subschemas of a property (through anyOf and array items)."""
    if "anyOf" in schema:
        return [s for sub in schema["anyOf"] for s in _string_schemas(sub)]
    if schema.get("type") == "array":
        return _string_schemas(schema.get("items", {}))
    return [schema] if schema.get("type") == "string" else []


def test_every_id_parameter_requires_digits() -> None:
    checked = 0
    for name, tool in _tools().items():
        schema = tool.input_schema
        props = dict(schema["properties"])
        for row_name, row in (schema.get("$defs") or {}).items():
            props.update({f"{row_name}.{k}": v for k, v in row["properties"].items()})
        for prop, sub in props.items():
            base = prop.rsplit(".", 1)[-1]
            is_id = base in ID_PARAMS or base in ("rubric_item_ids", "expected_graded_ids")
            strings = _string_schemas(sub)
            if is_id:
                assert strings, (name, prop)
                assert all(s.get("pattern") == "^[0-9]+$" for s in strings), (name, prop)
                checked += 1
            else:
                assert not base.endswith("_id"), (name, prop)
                assert all("pattern" not in s for s in strings), (name, prop)
    assert checked > 80


def test_numeric_and_padded_ids_are_accepted_and_normalized(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(server, "get_assignments", lambda course_id: seen.append(course_id) or "ok")

    for value in (123, "123", " 123 ", "`123`"):
        result = _call("tool_get_assignments", {"course_id": value})
        assert result.is_error is False

    assert seen == ["123"] * 4


@pytest.mark.parametrize(
    "value", ["../1", "1/../2", "12/grade", "", "  ", "abc", "1e3", "-1", -1, 1.5, True, None, ["1"]],
)
def test_invalid_ids_are_rejected_before_the_tool_runs(monkeypatch, value) -> None:
    monkeypatch.setattr(server, "get_assignments", lambda *_a: pytest.fail("tool ran"))

    with pytest.raises(ToolError, match="course_id"):
        _call("tool_get_assignments", {"course_id": value})


# Arabic-Indic, fullwidth, superscript, Devanagari and mixed digits: Python's
# int() and a Unicode \d accept some of them, but they are not Gradescope IDs
# and would reach the request URL percent-encoded.
@pytest.mark.parametrize("value", ["\u0661\u0662\u0663", "\uff11\uff12\uff13", "\u00b2",
                                   "\u0967\u0968", "1\u0663", " \uff11 "])
def test_non_ascii_digit_ids_are_rejected(monkeypatch, value) -> None:
    monkeypatch.setattr(server, "get_assignments", lambda *_a: pytest.fail("tool ran"))
    monkeypatch.setattr(server, "apply_grade", lambda *_a: pytest.fail("tool ran"))

    with pytest.raises(ToolError, match="ASCII digits"):
        _call("tool_get_assignments", {"course_id": value})
    with pytest.raises(ToolError, match="rubric_item_ids"):
        _call("tool_apply_grade", {
            "course_id": "1", "question_id": "2", "submission_id": "3",
            "rubric_item_ids": ["10", value],
        })
    with pytest.raises(ResourceNotFoundError):
        _read(f"gradescope://courses/{value}/assignments")


@pytest.mark.parametrize("value, expected", [
    ("031", "31"), ("`0031`", "31"), (" 0100 ", "100"), ("0", "0"), ("000", "0"),
    (0, "0"), (31, "31"),
])
def test_ids_are_canonicalized_without_leading_zeros(monkeypatch, value, expected) -> None:
    seen = []
    monkeypatch.setattr(server, "get_assignments", lambda course_id: seen.append(course_id) or "ok")

    result = _call("tool_get_assignments", {"course_id": value})

    assert result.is_error is False
    assert seen == [expected]


def test_leading_zero_ids_are_canonical_in_rubric_lists_and_resources(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(server, "apply_grade", lambda *args: seen.setdefault("apply", args) and "ok")
    monkeypatch.setattr(server, "get_assignments", lambda cid: seen.setdefault("resource", cid))

    _call("tool_apply_grade", {
        "course_id": "01", "question_id": "02", "submission_id": "0031",
        "rubric_item_ids": ["0100", 200],
    })
    _read("gradescope://courses/042/assignments")

    assert seen["apply"][:4] == ("1", "2", "31", ["100", "200"])
    assert seen["resource"] == "42"


def test_leading_zero_submission_ids_are_duplicates_in_a_batch(monkeypatch) -> None:
    """'31' and '031' name one submission: refuse the batch instead of writing twice."""
    monkeypatch.setattr(grading_ops, "get_connection", lambda: pytest.fail("Gradescope contacted"))

    result = _call("tool_apply_grade_batch", {
        "course_id": "1", "question_id": "2", "confirm_write": True, "grades": [
            {"submission_id": "31", "rubric_item_ids": ["100"]},
            {"submission_id": "031", "rubric_item_ids": ["200"]},
        ],
    })

    assert result.is_error is True
    text = result.content[0].text
    assert text.startswith("Error: invalid batch input:")
    assert "row 1 (31): duplicate submission_id (also in row 0)" in text


# ------------------------------------------------------------------
# Number arguments
# ------------------------------------------------------------------

_GRADE = {"course_id": "1", "question_id": "2", "submission_id": "3", "rubric_item_ids": ["10"]}
_GROUP = {"course_id": "1", "question_id": "2", "group_id": "4", "rubric_item_ids": ["10"]}
_RUBRIC = {"course_id": "1", "question_id": "2", "description": "d"}

# (tool, implementation, fixed arguments, number parameter)
_NUMBER_PARAMS = [
    ("tool_apply_grade", "apply_grade", _GRADE, "point_adjustment"),
    ("tool_apply_grade", "apply_grade", _GRADE, "confidence"),
    ("tool_grade_answer_group", "grade_answer_group", _GROUP, "point_adjustment"),
    ("tool_grade_answer_group", "grade_answer_group", _GROUP, "expected_member_count"),
    ("tool_create_rubric_item", "create_rubric_item", _RUBRIC, "weight"),
    ("tool_update_rubric_item", "update_rubric_item", {**_RUBRIC, "rubric_item_id": "5"}, "weight"),
]


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("tool, impl, base, param", _NUMBER_PARAMS)
def test_number_parameters_reject_booleans(monkeypatch, tool, impl, base, param, value) -> None:
    """JSON true/false must not become 1/0 (a +1 adjustment, confidence 1.0, ...)."""
    monkeypatch.setattr(server, impl, lambda *_a, **_k: pytest.fail("tool ran"))

    with pytest.raises(ToolError, match=rf"{param}[\s\S]*not a boolean"):
        _call(tool, {**base, param: value})


@pytest.mark.parametrize("tool, impl, base, param", _NUMBER_PARAMS)
def test_number_parameters_accept_numbers_and_numeric_strings(
    monkeypatch, tool, impl, base, param
) -> None:
    sig = inspect.signature(getattr(server, impl))
    seen = []
    monkeypatch.setattr(
        server, impl,
        lambda *a, **k: seen.append(sig.bind(*a, **k).arguments[param]) or "ok",
    )
    if param == "expected_member_count":
        cases = [(3, 3), ("3", 3), (0, 0)]
    else:
        cases = [(2, 2.0), (0.75, 0.75), ("0.75", 0.75), (-1.5, -1.5)]

    for value, expected in cases:
        result = _call(tool, {**base, param: value})
        assert result.is_error is False, (value, result.content[0].text)
    assert seen == [expected for _value, expected in cases]
    assert all(type(v) is type(cases[0][1]) for v in seen)


@pytest.mark.parametrize("field", ["point_adjustment", "confidence"])
@pytest.mark.parametrize("value", [True, False])
def test_batch_row_numbers_reject_booleans(monkeypatch, field, value) -> None:
    monkeypatch.setattr(server, "apply_grade_batch", lambda *_a, **_k: pytest.fail("tool ran"))

    with pytest.raises(ToolError, match=rf"grades[\s\S]*{field}[\s\S]*not a boolean"):
        _call("tool_apply_grade_batch", {"course_id": "1", "question_id": "2", "grades": [
            {"submission_id": "5", "rubric_item_ids": ["10"], field: value},
        ]})


def test_boolean_adjustment_never_reaches_gradescope(monkeypatch) -> None:
    """The reviewer's repro: point_adjustment=true used to be saved as +1 point."""
    monkeypatch.setattr(grading_ops, "get_connection", lambda: pytest.fail("Gradescope contacted"))

    for args in (
        {**_GRADE, "point_adjustment": True, "confirm_write": True},
        {**_GRADE, "confidence": True, "confirm_write": True},
    ):
        with pytest.raises(ToolError, match="not a boolean"):
            _call("tool_apply_grade", args)
    with pytest.raises(ToolError, match="not a boolean"):
        _call("tool_apply_grade_batch", {
            "course_id": "1", "question_id": "2", "confirm_write": True,
            "grades": [{"submission_id": "5", "rubric_item_ids": ["10"], "confidence": False}],
        })


def test_rubric_item_id_lists_are_validated_elementwise(monkeypatch) -> None:
    seen = {}

    def fake_apply_grade(*args):
        seen["rubric_item_ids"] = args[3]
        return "ok"

    monkeypatch.setattr(server, "apply_grade", fake_apply_grade)
    base = {"course_id": "1", "question_id": "2", "submission_id": "3"}

    _call("tool_apply_grade", {**base, "rubric_item_ids": [10, " `11` ", "12"]})
    assert seen["rubric_item_ids"] == ["10", "11", "12"]

    with pytest.raises(ToolError, match="rubric_item_ids"):
        _call("tool_apply_grade", {**base, "rubric_item_ids": ["10", "../x"]})


def test_blank_optional_ids_mean_not_given(monkeypatch) -> None:
    seen = {}

    def fake_next(course_id, question_id, submission_id, output_format):
        seen["next"] = submission_id
        return "ok"

    def fake_prepare(course_id, assignment_id, question_id, submission_id):
        seen["prepare"] = (assignment_id, submission_id)
        return "ok"

    monkeypatch.setattr(server, "get_next_ungraded", fake_next)
    monkeypatch.setattr(server, "prepare_grading_artifact", fake_prepare)

    _call("tool_get_next_ungraded", {"course_id": "1", "question_id": "2", "submission_id": " "})
    _call("tool_prepare_grading_artifact", {
        "course_id": "1", "question_id": "2", "assignment_id": "", "submission_id": None,
    })

    assert seen == {"next": "", "prepare": (None, None)}


def test_workflow_tools_require_their_ids() -> None:
    tools = _tools()
    required = {name: tools[name].input_schema["required"] for name in (
        "tool_prepare_grading_artifact", "tool_assess_submission_readiness",
        "tool_cache_relevant_pages", "tool_smart_read_submission",
    )}
    assert required == {
        "tool_prepare_grading_artifact": ["course_id", "question_id"],
        "tool_assess_submission_readiness": ["course_id", "question_id", "submission_id"],
        "tool_cache_relevant_pages": ["course_id", "question_id", "submission_id"],
        "tool_smart_read_submission": ["course_id", "question_id", "submission_id"],
    }
    with pytest.raises(ToolError, match="submission_id"):
        _call("tool_smart_read_submission", {"course_id": "1", "question_id": "2"})


def test_grade_answer_group_requires_rubric_item_ids() -> None:
    schema = _tools()["tool_grade_answer_group"].input_schema
    assert "rubric_item_ids" in schema["required"]
    assert schema["properties"]["expected_member_count"]["anyOf"][0]["minimum"] == 0


# ------------------------------------------------------------------
# Enums and typed batch rows
# ------------------------------------------------------------------


def test_output_format_and_filter_are_enums() -> None:
    tools = _tools()
    with_format = {
        n for n, t in tools.items() if "output_format" in t.input_schema["properties"]
    }
    assert with_format == {
        "tool_export_assignment_scores", "tool_get_submission_grading_context",
        "tool_get_next_ungraded", "tool_get_answer_groups", "tool_get_answer_group_detail",
    }
    for name in with_format:
        prop = tools[name].input_schema["properties"]["output_format"]
        assert prop["enum"] == ["markdown", "json"] and prop["default"] == "markdown", name

    prop = tools["tool_list_question_submissions"].input_schema["properties"]["filter"]
    assert prop["enum"] == ["all", "ungraded", "graded"]

    with pytest.raises(ToolError, match="output_format"):
        _call("tool_export_assignment_scores",
              {"course_id": "1", "assignment_id": "2", "output_format": "csv"})
    with pytest.raises(ToolError, match="filter"):
        _call("tool_list_question_submissions",
              {"course_id": "1", "question_id": "2", "filter": "pending"})


def test_grading_context_passes_output_format(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(
        server, "get_submission_grading_context",
        lambda *args: seen.setdefault("args", args) and "{}",
    )

    _call("tool_get_submission_grading_context", {
        "course_id": "1", "question_id": "2", "submission_id": "3", "output_format": "json",
    })

    assert seen["args"] == ("1", "2", "3", "json")


def test_batch_rows_are_typed_and_strict() -> None:
    schema = _tools()["tool_apply_grade_batch"].input_schema
    row = schema["$defs"]["GradeRow"]

    assert schema["properties"]["grades"]["items"] == {"$ref": "#/$defs/GradeRow"}
    assert row["additionalProperties"] is False
    assert row["required"] == ["submission_id"]
    assert set(row["properties"]) == {
        "submission_id", "rubric_item_ids", "point_adjustment", "comment", "confidence",
        "overwrite",
    }
    # Overwrite approval is per row; there is no batch-wide flag.
    assert "overwrite_graded" not in schema["properties"]


def test_batch_rows_reach_the_implementation_as_plain_dicts(monkeypatch) -> None:
    seen = {}

    def fake_batch(course_id, question_id, grades, confirm_write):
        seen["grades"] = grades
        seen["confirm_write"] = confirm_write
        return "preview"

    monkeypatch.setattr(server, "apply_grade_batch", fake_batch)

    _call("tool_apply_grade_batch", {"course_id": "1", "question_id": "2", "grades": [
        {"submission_id": 55, "rubric_item_ids": [300]},
        {"submission_id": "56", "comment": None, "confidence": "0.9", "overwrite": True},
    ]})

    # Only the keys the client sent, so omitted fields keep their current values.
    assert seen["grades"] == [
        {"submission_id": "55", "rubric_item_ids": ["300"]},
        {"submission_id": "56", "comment": None, "confidence": 0.9, "overwrite": True},
    ]
    assert all(type(g) is dict for g in seen["grades"])
    assert seen["confirm_write"] is False


@pytest.mark.parametrize("row", [
    {"submission_id": "1", "rubric_items": ["2"]},
    {"rubric_item_ids": ["2"]},
    {"submission_id": "1/../2", "comment": "x"},
    {"submission_id": "1", "rubric_item_ids": ["abc"]},
])
def test_bad_batch_rows_are_rejected_before_the_tool_runs(monkeypatch, row) -> None:
    monkeypatch.setattr(server, "apply_grade_batch", lambda *_a: pytest.fail("tool ran"))

    with pytest.raises(ToolError, match="grades"):
        _call("tool_apply_grade_batch", {"course_id": "1", "question_id": "2", "grades": [row]})


def test_batch_size_cap_is_advertised_and_enforced_before_any_request(monkeypatch) -> None:
    prop = _tools()["tool_apply_grade_batch"].input_schema["properties"]["grades"]
    assert prop["maxItems"] == MAX_BATCH_ROWS == 50

    seen = []
    monkeypatch.setattr(server, "apply_grade_batch", lambda c, q, grades, *_a, **_k: seen.append(
        len(grades)) or "preview")
    rows = [{"submission_id": str(1000 + i), "rubric_item_ids": ["1"]} for i in range(51)]
    _call("tool_apply_grade_batch", {"course_id": "1", "question_id": "2", "grades": rows[:50]})
    assert seen == [50]

    # Over the cap: the implementation refuses before contacting Gradescope.
    monkeypatch.setattr(server, "apply_grade_batch", grading_ops.apply_grade_batch)
    monkeypatch.setattr(grading_ops, "get_connection", lambda: pytest.fail("Gradescope contacted"))
    result = _call("tool_apply_grade_batch", {
        "course_id": "1", "question_id": "2", "grades": rows, "confirm_write": True,
    })
    assert result.is_error is True
    assert result.content[0].text.startswith("Error: grades has 51 rows; at most 50 rows")


# ------------------------------------------------------------------
# Wrapper <-> implementation parity
# ------------------------------------------------------------------


def _impl(tool_name: str):
    return getattr(server, tool_name.removeprefix("tool_"))


def test_wrappers_expose_every_implementation_parameter() -> None:
    for name, tool in _tools().items():
        impl_params = set(inspect.signature(_impl(name)).parameters)
        assert set(tool.input_schema["properties"]) == impl_params, name


def _sample(prop: str, schema: dict, defs: dict, counter: list[int]):
    """A non-default value for a property, and what the implementation should receive."""
    if "anyOf" in schema:
        schema = next(s for s in schema["anyOf"] if s.get("type") != "null")
    if "$ref" in schema:
        raise AssertionError(prop)
    if "enum" in schema:
        value = schema["enum"][-1]
        return value, value
    kind = schema.get("type")
    if kind == "string":
        counter[0] += 1
        if schema.get("pattern"):
            return counter[0], str(counter[0])  # numbers are converted
        return f"{prop}-value", f"{prop}-value"
    if kind == "boolean":
        return True, True
    if kind == "number":
        return 2.5, 2.5
    if kind == "integer":
        return 4, 4
    if kind == "array":
        items = schema["items"]
        if "$ref" in items:
            return (
                [{"submission_id": 7, "rubric_item_ids": [8], "comment": "c"}],
                [{"submission_id": "7", "rubric_item_ids": ["8"], "comment": "c"}],
            )
        value, expected = _sample(prop, items, defs, counter)
        return [value], [expected]
    raise AssertionError((prop, schema))


def test_wrappers_pass_every_argument_to_the_right_parameter(monkeypatch) -> None:
    for name, tool in _tools().items():
        impl = _impl(name)
        sig = inspect.signature(impl)
        seen = {}

        def fake(*args, _sig=sig, _seen=seen, **kwargs):
            _seen.update(_sig.bind(*args, **kwargs).arguments)
            return "ok"

        monkeypatch.setattr(server, impl.__name__, fake)
        schema = tool.input_schema
        counter = [100]
        args, expected = {}, {}
        for prop, sub in schema["properties"].items():
            if prop == "confirm_write":
                args[prop], expected[prop] = False, False  # never write here
                continue
            args[prop], expected[prop] = _sample(prop, sub, schema.get("$defs", {}), counter)

        result = _call(name, args)

        assert result.is_error is False, (name, result.content[0].text)
        assert seen == expected, name


# ------------------------------------------------------------------
# Error signalling
# ------------------------------------------------------------------


@pytest.mark.parametrize("text, is_error", [
    ("Error: course_id is required.", True),
    ("Error fetching extensions: Gradescope returned HTTP 500", True),
    ("  Error: leading whitespace", True),
    ("Authentication error: Missing Gradescope credentials.", True),
    ("❌ Gradescope rejected the rename", True),
    ("Errors are listed below", False),
    ("Write confirmation required for `rename_assignment`.\nNo changes were made.", False),
    ("⚠️ **Grade REJECTED** — Your confidence is `0.40`", False),
    ("⚠️ Extension was submitted (HTTP 200) but could not be verified", False),
    ("No courses found for this account.", False),
    ("## Assignments for Course 1", False),
])
def test_error_prefixes_set_is_error_and_keep_the_text(monkeypatch, text, is_error) -> None:
    monkeypatch.setattr(server, "list_courses", lambda: text)

    result = _call("tool_list_courses", {})

    assert result.is_error is is_error
    assert [c.text for c in result.content] == [text]
    assert result.structured_content is None


def test_escaped_auth_error_is_an_error_result(monkeypatch) -> None:
    def boom():
        raise AuthError("Gradescope login failed: invalid credentials.")

    monkeypatch.setattr(server, "list_courses", boom)

    result = _call("tool_list_courses", {})

    assert result.is_error is True
    assert result.content[0].text == (
        "Authentication error: Gradescope login failed: invalid credentials."
    )


def test_is_error_reaches_the_client_over_the_protocol(monkeypatch) -> None:
    monkeypatch.setattr(server, "rename_assignment", lambda *_a: "❌ Gradescope rejected the rename.")

    async def session():
        async with Client(server.mcp) as client:
            listed = await client.list_tools()
            failed = await client.call_tool("tool_list_courses", {})  # no credentials
            rejected = await client.call_tool(
                "tool_rename_assignment",
                {"course_id": 1, "assignment_id": "2", "new_title": "X", "confirm_write": True},
            )
            invalid = await client.call_tool("tool_get_assignments", {"course_id": "../1"})
            preview = await client.call_tool(
                "tool_upload_submission",
                {"course_id": "1", "assignment_id": "2", "file_paths": ["relative.pdf"]},
            )
            return listed, failed, rejected, invalid, preview

    listed, failed, rejected, invalid, preview = anyio.run(session)

    apply_grade = next(t for t in listed.tools if t.name == "tool_apply_grade")
    assert apply_grade.annotations.destructive_hint is True
    assert apply_grade.output_schema is None
    assert failed.is_error is True
    assert failed.content[0].text.startswith("Authentication error:")
    assert failed.structured_content is None
    assert rejected.is_error is True
    assert rejected.content[0].text == "❌ Gradescope rejected the rename."
    assert invalid.is_error is True
    assert "must be a numeric Gradescope ID" in invalid.content[0].text
    assert preview.is_error is True  # 'Error: ... file path must be absolute'
    assert "must be absolute" in preview.content[0].text


# ------------------------------------------------------------------
# Resources
# ------------------------------------------------------------------


def _read(uri: str):
    async def read():
        return list(await server.mcp.read_resource(uri))

    return anyio.run(read)


@pytest.mark.parametrize("template", ["assignments", "roster"])
def test_resource_templates_validate_course_id(monkeypatch, template) -> None:
    seen = []
    monkeypatch.setattr(server, "get_assignments", lambda cid: seen.append(cid) or "assignments")
    monkeypatch.setattr(server, "get_course_roster", lambda cid: seen.append(cid) or "roster")

    contents = _read(f"gradescope://courses/42/{template}")
    assert contents[0].content == template
    for bad in ("abc", "1%2F..%2F2", "-1"):
        with pytest.raises(ResourceNotFoundError):
            _read(f"gradescope://courses/{bad}/{template}")

    assert seen == ["42"]


def test_resource_failure_text_becomes_a_resource_error(monkeypatch) -> None:
    monkeypatch.setattr(server, "list_courses", lambda: "Authentication error: no credentials.")

    with pytest.raises(ResourceError, match="^Authentication error: no credentials.$"):
        _read("gradescope://courses")


# ------------------------------------------------------------------
# Docstrings and prompts
# ------------------------------------------------------------------


def test_apply_grade_docs_match_confidence_thresholds() -> None:
    reject, review = f"{CONFIDENCE_REJECT_BELOW:g}", f"{CONFIDENCE_REVIEW_UP_TO:g}"
    tools = _tools()
    apply_doc = " ".join(tools["tool_apply_grade"].description.split())
    batch_doc = " ".join(tools["tool_apply_grade_batch"].description.split())

    assert f"< {reject} rejected" in apply_doc
    assert f"{reject}-{review} inclusive written but flagged NEEDS HUMAN REVIEW" in apply_doc
    assert f"< {reject} is skipped; {reject}-{review} inclusive is written" in batch_doc


def test_tool_descriptions_do_not_hardcode_the_old_cache_path() -> None:
    for name, tool in _tools().items():
        assert "/tmp/gradescope-mcp" not in tool.description, name


def test_descriptions_state_the_implementation_limits() -> None:
    from gradescope_mcp.tools import assignments, grading_workflow

    tools = _tools()

    def doc(name):
        return " ".join(tools[name].description.split())

    mb = grading_workflow._MAX_PAGE_BYTES / (1024 * 1024)
    deadline = grading_workflow._PAGE_DEADLINE_SECONDS
    assert f"exceeds {mb:g} MB or takes more than {deadline:g} s" in doc("tool_cache_relevant_pages")
    wait = f"waits up to {assignments._WRITE_LOCK_TIMEOUT:g} s"
    assert wait in doc("tool_set_extension") and wait in doc("tool_modify_assignment_dates")
    assert f"At most {MAX_BATCH_ROWS} rows per call" in doc("tool_apply_grade_batch")


def test_descriptions_make_no_claims_about_a_client_harness() -> None:
    """The server can't know or enforce what a client lets subagents call."""
    for name, tool in _tools().items():
        text = " ".join(tool.description.split())
        assert "Claude Code" not in text and "harness" not in text, name
        assert "Subagents cannot" not in text, name
    batch = " ".join(_tools()["tool_apply_grade_batch"].description.split())
    assert "subagents (if any) should only propose rows" in batch
    assert "At most 50 rows per call" in batch


def test_descriptions_document_the_untrusted_block_id() -> None:
    doc = " ".join(_tools()["tool_get_student_submission"].description.split())
    assert "block id" in doc and "same random block id closes a block" in doc


def test_descriptions_match_the_round_two_behavior() -> None:
    tools = _tools()

    def doc(name):
        return " ".join(tools[name].description.split())

    assert "Only the dates passed are sent" not in doc("tool_set_extension")
    assert "every other current setting" in doc("tool_set_extension")
    assert "Other Settings" in doc("tool_get_extensions")
    assert "not found in course" in doc("tool_get_assignment_details")
    assert "Upload not confirmed" in doc("tool_upload_submission")
    assert "unknown is not the same as ungraded" in doc("tool_get_assignment_submissions")
    assert "crop matches none of the pages" in doc("tool_get_regrade_detail")
    assert "unknown" in doc("tool_get_question_rubric")


_PROMPT_ARGS = {
    "course_id": "101",
    "assignment_id": "202",
    "question_id": "303",
    "student_email": "student@example.edu",
}


def _render_prompts() -> dict[str, str]:
    async def render():
        out = {}
        for prompt in await server.mcp.list_prompts():
            args = {a.name: _PROMPT_ARGS[a.name] for a in prompt.arguments or []}
            result = await server.mcp.get_prompt(prompt.name, args)
            out[prompt.name] = "\n".join(m.content.text for m in result.messages)
        return out

    return anyio.run(render)


def test_prompts_preview_and_get_approval_before_any_write() -> None:
    prompts = _render_prompts()
    assert len(prompts) == 7
    for name, text in prompts.items():
        assert "/tmp/gradescope-mcp" not in text, name
        write = text.find("confirm_write=True")
        if write == -1:
            continue
        preview = text.find("confirm_write=False")
        approval = text.lower().find("approv", preview)
        assert 0 <= preview < approval < write, name


def test_auto_grade_prompt_flow_and_thresholds() -> None:
    text = _render_prompts()["auto_grade_question"]
    reject, review = f"{CONFIDENCE_REJECT_BELOW:g}", f"{CONFIDENCE_REVIEW_UP_TO:g}"

    assert f"Below {reject}" in text
    assert f"{reject} to {review} inclusive" in text and "NEEDS HUMAN REVIEW" in text
    assert "Do not write comments unless I ask" in text
    assert "the path the tool prints" in text
    assert "not grading confidence" in text
    assert "UNTRUSTED" in text
    # The only write is the approved batch; never a direct tool_apply_grade write.
    assert "tool_apply_grade(" not in text
    order = [
        text.index("tool_apply_grade_batch(course_id="),  # preview
        text.index("explicit approval"),
        text.index("Only after I approve"),
        text.index("Step 7 — Verify"),
    ]
    assert order == sorted(order)


def test_other_prompts_use_tools_that_provide_what_they_promise() -> None:
    prompts = _render_prompts()

    stats = prompts["check_submission_stats"]
    assert "tool_export_assignment_scores" in stats and "tool_get_assignment_submissions" in stats

    regrades = prompts["review_regrade_requests"]
    assert "❓" in regrades and "never skip" in regrades
    assert "UNTRUSTED" in regrades and "never instructions" in regrades
    assert "tool_get_question_rubric" in regrades
    assert "outline to understand the rubric" not in regrades

    grade = prompts["grade_submission_with_rubric"]
    assert "tool_get_student_submission_map" in grade
    assert "Question Submission ID" in grade

    progress = prompts["summarize_course_progress"]
    assert "instructor" in progress.lower() and "tool_get_grading_progress" in progress

    extensions = prompts["manage_extensions_workflow"]
    assert "confirm_write=False" in extensions
    assert "Only the dates passed are sent" not in extensions
    assert "re-sends them unchanged" in extensions
    # A timezone argument is refused when several or unknown zones are
    # reported, so the prompt must not send the agent down that path.
    assert "give every date with a UTC offset instead" in extensions
    assert "course timezone is unknown, ask me for it" not in extensions

    assert "do not count those submissions as ungraded" in stats


def _after(text: str, first: str, then: str) -> bool:
    start = text.find(first)
    return start != -1 and text.find(then, start) > start


def test_grade_prompts_require_approval_for_overwrite_graded() -> None:
    prompts = _render_prompts()

    # A regraded submission is already graded: the previewed call carries
    # overwrite_graded=True and is repeated unchanged after approval.
    regrades = prompts["review_regrade_requests"]
    assert _after(regrades, "overwrite_graded=True, confirm_write=False", "explicit approval")
    assert _after(regrades, "explicit approval", "repeat the same call with confirm_write=True")
    assert "block id" in regrades

    grade = prompts["grade_submission_with_rubric"]
    assert _after(grade, "confirm_write=True alone will not write",
                  "overwrite_graded=True only if I explicitly approve")

    # Batch overwrite approval is per row: the prompt never suggests a
    # batch-wide flag, and rows the preview SKIPPED are left out.
    auto = prompts["auto_grade_question"]
    assert f"at most {MAX_BATCH_ROWS} rows per call" in auto
    assert "overwrite_graded" not in auto
    assert 'Already graded rows are SKIPPED unless the row has "overwrite": true' in auto
    assert _after(auto, "I explicitly approve overwriting",
                  'add "overwrite": true to that row (never to other rows)')
    assert "exactly as previewed (each row's overwrite key included)" in auto
    assert "Leave out the rows the preview marked SKIPPED" in auto
    assert "SKIPPED, OVERWRITTEN or NEEDS HUMAN REVIEW" in auto
    assert "'Not written: already graded at write time'" in auto
    assert "already holding the requested grade" in auto
    assert '"overwrite": true goes only on the rows whose grades I approved overwriting' in auto
    assert "block id" in auto
