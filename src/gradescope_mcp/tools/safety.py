"""Safety helpers for write-capable MCP tools."""


def write_confirmation_required(action: str, details: list[str]) -> str:
    """Return a standardized no-op preview for mutating tools."""
    lines = [
        f"Write confirmation required for `{action}`.",
        "No changes were made.",
    ]
    lines.extend(f"- {detail}" for detail in details)
    lines.append(
        "- Show this preview to the user; only after they explicitly approve, "
        "re-run with `confirm_write=True` to execute this change."
    )
    return "\n".join(lines)
