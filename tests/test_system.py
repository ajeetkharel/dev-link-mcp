import pytest
from conftest import text


@pytest.mark.bwrap
async def test_workspace_info_reports_sandbox_and_toolchains(connect, workspace):
    async with connect("system") as client:
        result = await client.call_tool("workspace_info", {})
    assert not result.is_error
    out = text(result)
    assert str(workspace) in out
    assert "sandbox: bubblewrap" in out
    assert "git:" in out


async def test_tool_calls_are_audited(connect, rt):
    async with connect("system") as client:
        await client.call_tool("server_status", {})
    events = rt.audit.query(kind="tool")
    assert [e["name"] for e in events] == ["server_status"]
    assert events[0]["status"] == "ok"
