"""Offline inventory of the installed MCP surface: python -m gradescope_mcp.inspect.

Reads the actual public registry, never loads credentials or calls Gradescope.
"""

import json
from importlib.metadata import version

import anyio


def inventory() -> dict:
    from gradescope_mcp.server import mcp

    async def collect():
        tools = await mcp.list_tools()
        resources = await mcp.list_resources()
        templates = await mcp.list_resource_templates()
        prompts = await mcp.list_prompts()
        groups = {"read": [], "gradescope_write": [], "local_cache": []}
        for tool in tools:
            hints = tool.annotations
            group = (
                "read"
                if hints.read_only_hint
                else "gradescope_write"
                if hints.destructive_hint
                else "local_cache"
            )
            groups[group].append(
                {
                    "name": tool.name,
                    "title": tool.title,
                    "idempotent": hints.idempotent_hint,
                    "input_schema": tool.input_schema,
                }
            )
        return {
            "versions": {
                name: version(name)
                for name in (
                    "gradescopeapi",
                    "mcp",
                    "beautifulsoup4",
                    "python-dotenv",
                    "urllib3",
                )
            },
            "counts": {
                **{key: len(value) for key, value in groups.items()},
                "tools": len(tools),
                "resources": len(resources) + len(templates),
                "prompts": len(prompts),
            },
            "tools": groups,
            "resources": [str(r.uri) for r in resources]
            + [t.uri_template for t in templates],
            "prompts": [
                {"name": p.name, "description": p.description} for p in prompts
            ],
        }

    return anyio.run(collect)


def main():
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
