from gradescope_mcp.inspect import inventory


def test_inventory_is_offline_and_matches_public_registry(monkeypatch):
    from gradescope_mcp import auth

    def forbidden():
        raise AssertionError("Inspection must not authenticate")

    monkeypatch.setattr(auth, "get_connection", forbidden)
    result = inventory()
    assert result["counts"]["tools"] == sum(
        len(group) for group in result["tools"].values()
    )
    writes = result["tools"]["gradescope_write"]
    assert all("confirm_write" in t["input_schema"]["properties"] for t in writes)
    assert any(t["name"] == "tool_update_assignment_outline" for t in writes)
    assert result["versions"]["mcp"]
