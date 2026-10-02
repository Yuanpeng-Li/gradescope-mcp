"""Cheap guards against README.md / AGENT.md / SKILL.md drifting from the code.

The inventories come from the live ``MCPServer`` registration and the
environment variables from the source tree. The docs are parsed loosely
(backticked names, table rows, numbered lists), so rewording prose does not
break these tests, but adding, removing or renaming a tool, prompt, resource,
parameter or environment variable without updating the docs does.
"""

from __future__ import annotations

import pathlib
import re

import anyio
import pytest

from gradescope_mcp import auth, server
from gradescope_mcp.tools import assignments, grading_workflow
from gradescope_mcp.tools.common import format_untrusted
from gradescope_mcp.tools.grading_ops import (
    CONFIDENCE_REJECT_BELOW,
    CONFIDENCE_REVIEW_UP_TO,
    MAX_BATCH_ROWS,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
AGENT = ROOT / "AGENT.md"
SKILL = ROOT / "skills" / "gradescope-assisted-grading" / "SKILL.md"
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"

AGENT_DOCS = [README, AGENT, SKILL]

_TOOL_NAME_RE = re.compile(r"\btool_[a-z0-9_]+")
_TOOL_ROW_RE = re.compile(r"^\|\s*`(tool_[a-z0-9_]+)`\s*\|")
_AGENT_ITEM_RE = re.compile(r"^\d+\.\s+`(tool_[a-z0-9_]+)`\s*$")
_TOOL_CALL_RE = re.compile(r"\b(tool_[a-z0-9_]+)\(([^)]*)\)")
_KWARG_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\s*=")
_ENV_NAME_RE = re.compile(r"[\"'](GRADESCOPE_[A-Z0-9_]+)[\"']")


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def registry():
    async def collect():
        return (
            await server.mcp.list_tools(),
            await server.mcp.list_resources(),
            await server.mcp.list_resource_templates(),
            await server.mcp.list_prompts(),
        )

    tools, resources, templates, prompts = anyio.run(collect)
    return {
        "tools": {tool.name: tool for tool in tools},
        "resource_uris": [str(r.uri) for r in resources]
        + [t.uri_template for t in templates],
        "prompts": {prompt.name for prompt in prompts},
    }


def _kind(tool) -> str:
    hints = tool.annotations
    if hints.read_only_hint:
        return "read-only"
    if hints.destructive_hint:
        return "write"
    return "local cache"


def _section(text: str, heading: str) -> str:
    """Return the body of a ``## heading`` section (up to the next ``## ``)."""
    match = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"missing section '## {heading}'"
    return match.group(1)


def _subsection(text: str, heading: str) -> str:
    """Return the body of a ``### heading`` subsection (up to the next heading)."""
    match = re.search(
        rf"^### {re.escape(heading)}\s*$(.*?)(?=^##+ |\Z)", text, re.M | re.S
    )
    assert match, f"missing subsection '### {heading}'"
    return match.group(1)


def _flat(text: str) -> str:
    """Collapse whitespace so wrapped prose matches a one-line phrase."""
    return " ".join(text.split())


# ------------------------------------------------------------------
# Counts
# ------------------------------------------------------------------


@pytest.mark.parametrize("path", [README, AGENT], ids=lambda p: p.name)
def test_stated_counts_match_the_registration(path, registry) -> None:
    text = _text(path)
    actual = {
        "tools": len(registry["tools"]),
        "resources": len(registry["resource_uris"]),
        "prompts": len(registry["prompts"]),
    }
    for noun, count in actual.items():
        stated = [int(n) for n in re.findall(rf"\b(\d+)\s+(?:MCP\s+)?{noun}\b", text)]
        assert stated, f"{path.name} does not state the number of {noun}"
        assert set(stated) == {count}, f"{path.name}: {noun} stated as {stated}, actual {count}"


@pytest.mark.parametrize("path", [README, AGENT], ids=lambda p: p.name)
def test_readme_and_agent_do_not_pin_a_test_count(path) -> None:
    # The count changes with every fix; DEVLOG.md records it per session.
    assert not re.search(r"\b\d+\s+(?:automated\s+|offline\s+)?tests\b", _text(path))


# ------------------------------------------------------------------
# Inventories
# ------------------------------------------------------------------


def test_readme_tool_tables_list_every_tool_once_with_its_kind(registry) -> None:
    rows = [line for line in _text(README).splitlines() if _TOOL_ROW_RE.match(line)]
    names = [_TOOL_ROW_RE.match(line).group(1) for line in rows]
    assert sorted(names) == sorted(registry["tools"]), "README tool tables != registered tools"
    assert len(names) == len(set(names)), "a tool is listed twice in README"

    for line in rows:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        name, kind = cells[0].strip("`"), cells[1]
        assert kind == _kind(registry["tools"][name]), (name, kind)


def test_agent_inventory_groups_match_the_annotations(registry) -> None:
    inventory = _section(_text(AGENT), "Tool Inventory")
    groups = {"read-only": "### Read-only", "write": "### Gradescope writes",
              "local cache": "### Local cache writes"}
    listed: dict[str, str] = {}
    current = None
    for line in inventory.splitlines():
        for kind, heading in groups.items():
            if line.strip() == heading:
                current = kind
        match = _AGENT_ITEM_RE.match(line.strip())
        if match:
            assert current, f"{match.group(1)} is listed outside a group"
            assert match.group(1) not in listed, f"{match.group(1)} is listed twice"
            listed[match.group(1)] = current

    assert sorted(listed) == sorted(registry["tools"]), "AGENT.md inventory != registered tools"
    for name, kind in listed.items():
        assert kind == _kind(registry["tools"][name]), (name, kind)


def test_readme_documents_every_resource_and_prompt(registry) -> None:
    text = _text(README)
    for uri in registry["resource_uris"]:
        assert f"`{uri}`" in text, uri

    prompt_rows = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", _section(text, "Prompts"), re.M))
    assert prompt_rows == registry["prompts"]


# ------------------------------------------------------------------
# References from the docs
# ------------------------------------------------------------------


@pytest.mark.parametrize("path", AGENT_DOCS, ids=lambda p: p.name)
def test_docs_only_mention_registered_tools(path, registry) -> None:
    mentioned = set(_TOOL_NAME_RE.findall(_text(path)))
    unknown = mentioned - set(registry["tools"])
    assert not unknown, f"{path.name} mentions tools that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize("path", AGENT_DOCS, ids=lambda p: p.name)
def test_documented_keyword_arguments_exist(path, registry) -> None:
    problems = []
    for name, args in _TOOL_CALL_RE.findall(_text(path)):
        tool = registry["tools"].get(name)
        if tool is None:
            continue  # reported by test_docs_only_mention_registered_tools
        params = set(tool.input_schema.get("properties", {}))
        for kwarg in _KWARG_RE.findall(args):
            if kwarg not in params:
                problems.append(f"{name}({kwarg}=...)")
    assert not problems, f"{path.name} uses parameters the tools don't have: {problems}"


@pytest.mark.parametrize(
    "path", [README, AGENT, SKILL, ENV_EXAMPLE, GITIGNORE], ids=lambda p: p.name
)
def test_docs_do_not_point_at_the_old_shared_tmp_cache(path) -> None:
    # Artifacts live in a private per-user cache whose path the tools print.
    assert "/tmp/gradescope" not in _text(path)


def test_skill_install_targets_a_client_skills_directory() -> None:
    install = _section(_text(README), "Assisted Grading Skill")
    assert "~/.claude/skills" in install
    assert "/tmp" not in install


def test_environment_variables_are_documented() -> None:
    names = set()
    for source in (ROOT / "src").rglob("*.py"):
        names.update(_ENV_NAME_RE.findall(_text(source)))
    assert {"GRADESCOPE_EMAIL", "GRADESCOPE_PASSWORD", "GRADESCOPE_MCP_CACHE_DIR"} <= names

    readme, env_example = _text(README), _text(ENV_EXAMPLE)
    for name in sorted(names):
        assert name in readme, f"{name} is not documented in README.md"
        assert name in env_example, f"{name} is missing from .env.example"


def test_confidence_thresholds_in_docs_match_the_code() -> None:
    reject, review = f"{CONFIDENCE_REJECT_BELOW:g}", f"{CONFIDENCE_REVIEW_UP_TO:g}"
    for path in (README, SKILL):
        text = " ".join(_text(path).split())
        assert f"below {reject}" in text, path.name
        assert f"{reject} to {review} inclusive" in text, path.name


def test_project_tree_lists_every_module_and_test_file() -> None:
    readme, agent = _text(README), _text(AGENT)
    for source in sorted((ROOT / "src" / "gradescope_mcp").rglob("*.py")):
        assert source.name in readme, f"README project tree is missing {source.name}"
    for test_file in sorted((ROOT / "tests").glob("*.py")):
        assert test_file.name in readme, f"README project tree is missing {test_file.name}"
        if test_file.name.startswith("test_"):
            assert f"tests/{test_file.name}" in agent, f"AGENT.md is missing {test_file.name}"


def test_skill_frontmatter_names_the_skill_directory() -> None:
    match = re.match(r"---\n(.*?)\n---\n", _text(SKILL), re.S)
    assert match, "SKILL.md has no YAML frontmatter"
    assert re.search(r"^name:\s*gradescope-assisted-grading\s*$", match.group(1), re.M)
    assert re.search(r"^description:\s*\S", match.group(1), re.M)


# ------------------------------------------------------------------
# Interface rules and limits stated in the docs
# ------------------------------------------------------------------


def _number_parameters(schema: dict) -> set[str]:
    """Names of number/integer properties in a tool schema, including batch rows."""
    names: set[str] = set()
    tables = [schema.get("properties", {})]
    tables += [d.get("properties", {}) for d in schema.get("$defs", {}).values()]
    for properties in tables:
        for name, prop in properties.items():
            options = prop.get("anyOf", [prop])
            if any(option.get("type") in ("number", "integer") for option in options):
                names.add(name)
    return names


def test_documented_id_pattern_matches_the_schema(registry) -> None:
    schema = registry["tools"]["tool_get_assignments"].input_schema
    pattern = schema["properties"]["course_id"]["pattern"]
    ids = _subsection(_text(README), "IDs")
    assert f"`{pattern}`" in ids
    assert "leading zeros" in ids


def test_readme_lists_every_number_argument(registry) -> None:
    # Every number parameter rejects true/false (server.Number / server.Count);
    # the README section must name each one.
    names: set[str] = set()
    for tool in registry["tools"].values():
        names |= _number_parameters(tool.input_schema)
    assert names, "no number parameters found in the tool schemas"
    numbers = _subsection(_text(README), "Numbers")
    missing = sorted(name for name in names if f"`{name}`" not in numbers)
    assert not missing, f"README '### Numbers' does not mention {missing}"
    assert "`true` and `false` are rejected" in _flat(numbers)


@pytest.mark.parametrize(
    "path, phrase",
    [
        (README, f"at most {MAX_BATCH_ROWS} rows per call"),
        (SKILL, f"at most {MAX_BATCH_ROWS} rows per call"),
        (README, f"more than {assignments._WRITE_LOCK_TIMEOUT:g} s"),
        (README, f"exceeds {grading_workflow._MAX_PAGE_BYTES // (1024 * 1024)} MB"),
        (README, f"more than {grading_workflow._PAGE_DEADLINE_SECONDS:g} s"),
    ],
    ids=["readme-batch", "skill-batch", "write-lock", "page-bytes", "page-time"],
)
def test_documented_limits_match_the_code(path, phrase) -> None:
    assert phrase in _flat(_text(path)), f"{path.name} does not say '{phrase}'"


def _minutes(seconds: float) -> str:
    minutes = seconds / 60
    assert minutes == int(minutes), seconds
    return f"{int(minutes)} minute" + ("" if minutes == 1 else "s")


def test_login_cooldowns_in_readme_match_the_code() -> None:
    text = _flat(_section(_text(README), "Authentication"))
    for name, seconds, context in [
        ("invalid credentials", auth.INVALID_CREDENTIALS_COOLDOWN, "is wrong): "),
        ("throttled", auth.THROTTLED_LOGIN_COOLDOWN, "or "),
        ("too many attempts", auth.TOO_MANY_ATTEMPTS_COOLDOWN, "page: "),
        ("other rejection", auth.REJECTED_LOGIN_COOLDOWN, "...): "),
        ("cap", auth.MAX_LOGIN_COOLDOWN, "capped at "),
    ]:
        assert f"{context}{_minutes(seconds)}" in text, (name, _minutes(seconds))


def test_untrusted_block_markers_are_documented_as_rendered() -> None:
    lines = format_untrusted("answer", "LABEL").splitlines()
    begin, end = lines[0], lines[-1]
    block_id = re.search(r"block id ([0-9a-f]+)", begin).group(1)

    def template(line: str) -> str:
        return line.replace("LABEL", "{label}").replace(block_id, "{id}")

    readme = _flat(_text(README))
    assert template(begin).split(";", 1)[0] in readme  # "<<<BEGIN ... (block id {id}"
    assert template(end) in readme  # "<<<END UNTRUSTED {label}>>> (block id {id})"
    assert "block id" in _text(SKILL)


@pytest.mark.parametrize(
    "phrase",
    [
        "subagents cannot call",  # a client-harness claim the server can't back
        "non-zero weight",  # weight-0 leaf questions are kept in the answer key
        "only the dates passed are sent",  # set_extension re-sends the whole extension
        "until the server is restarted",  # login failures now have a cooldown
        "until the credentials change",
        "walks up",  # .env is no longer searched in parent directories
        "^\\d+$",  # IDs are ASCII digits, ^[0-9]+$
    ],
)
def test_docs_do_not_repeat_superseded_claims(phrase) -> None:
    for path in (README, AGENT, SKILL, ENV_EXAMPLE):
        assert phrase.lower() not in _flat(_text(path)).lower(), (path.name, phrase)
