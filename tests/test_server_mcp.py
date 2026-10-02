"""Tests that exercise the server through the mcp v2 ``MCPServer`` layer.

These pin the surface MCP clients actually see (registration counts,
argument validation, result shape) and the v2 execution model: sync tool
functions run on a worker thread instead of blocking the event loop.
"""

from __future__ import annotations

import threading

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from gradescope_mcp import server


def test_registers_expected_tools_resources_and_prompts() -> None:
    async def inventory():
        return (
            await server.mcp.list_tools(),
            await server.mcp.list_resources(),
            await server.mcp.list_resource_templates(),
            await server.mcp.list_prompts(),
        )

    tools, resources, templates, prompts = anyio.run(inventory)

    assert len(tools) == 37
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
    assert result.structured_content == {"result": "courses"}
    assert seen["thread"] is not seen["loop_thread"]


def test_write_tool_preview_through_mcp_layer() -> None:
    result = anyio.run(
        server.mcp.call_tool,
        "tool_rename_assignment",
        {"course_id": "1", "assignment_id": "2", "new_title": "Midterm"},
    )

    assert result.is_error is False
    assert "No changes were made." in result.content[0].text


def test_argument_validation_rejects_non_string_ids() -> None:
    with pytest.raises(ToolError, match="valid string"):
        anyio.run(server.mcp.call_tool, "tool_get_assignments", {"course_id": 123})
