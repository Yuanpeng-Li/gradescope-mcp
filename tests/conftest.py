"""Shared pytest fixtures.

Every test runs against a private, per-test artifact cache and without
Gradescope credentials, so the suite never writes into a shared location
(and never picks up artifacts a previous run or another user left there).
"""

from __future__ import annotations

import pytest

from gradescope_mcp.tools import grading_workflow


@pytest.fixture(autouse=True)
def _hermetic_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(tmp_path / "gradescope-mcp-cache"))
    monkeypatch.delenv("GRADESCOPE_EMAIL", raising=False)
    monkeypatch.delenv("GRADESCOPE_PASSWORD", raising=False)
    # The question -> assignment memo is process-wide; don't leak it between tests.
    grading_workflow._ASSIGNMENT_BY_QUESTION.clear()
    yield
    grading_workflow._ASSIGNMENT_BY_QUESTION.clear()
