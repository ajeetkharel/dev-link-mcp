"""Published tool schemas: no pydantic titles, and no output schema for tools that return plain text.

These also guard the SDK internals trim_tool_schemas relies on: if the SDK changes, a str tool
starts sending structuredContent again or a schema keeps its titles, and these tests fail.
"""

from __future__ import annotations

import json

import pytest
from conftest import text
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool

from dev_link_mcp.tools import BUILTINS, load_group_class, missing_requirements, trim_tool_schemas

INSTALLED = [b.name for b in BUILTINS if not missing_requirements(b)]


async def test_published_schemas_carry_no_titles(connect):
    async with connect(*INSTALLED) as client:
        tools = (await client.list_tools()).tools
    assert len(tools) > 50
    for tool in tools:
        for schema in (tool.input_schema, tool.output_schema):
            # A property called `title` maps to an object, so only title strings match.
            assert '"title": "' not in json.dumps(schema), tool.name
    task_add = next(t for t in tools if t.name == "task_add")
    assert "title" in task_add.input_schema["properties"]
    assert "title" in task_add.input_schema["required"]


async def test_str_tool_sends_text_once(connect, rt, workspace):
    (workspace / "a.txt").write_text("x")
    untrimmed = MCPServer("untrimmed")
    group = load_group_class(next(b.target for b in BUILTINS if b.name == "files"))
    group(rt, group.Settings()).register(untrimmed)
    async with Client(untrimmed) as client:
        before = await client.call_tool("list_dir", {"path": "."})
    # Without the trim the SDK repeats the text as structuredContent; that is what we remove.
    assert before.structured_content == {"result": text(before)}

    async with connect("files") as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        after = await client.call_tool("list_dir", {"path": "."})
        invalid = await client.call_tool("list_dir", {"path": ".", "show_hidden": "lots"})
    assert listed["list_dir"].output_schema is None
    assert not after.is_error and after.structured_content is None
    assert after.content == before.content
    assert invalid.is_error


async def test_structured_tools_keep_schema_and_data(connect, workspace):
    (workspace / "a.txt").write_text("x")
    async with connect("files", "checkpoints") as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        info = await client.call_tool("file_info", {"path": "a.txt"})
        checkpoints = await client.call_tool("checkpoint_list", {})
    for name in ("file_info", "checkpoint_list"):
        assert listed[name].output_schema == {"additionalProperties": True, "type": "object"}, name
    assert info.structured_content["size"] == 1
    assert checkpoints.structured_content["checkpoints"] == []
    assert text(info) and text(checkpoints)


async def test_trim_undoes_a_tool_whose_published_schema_would_not_follow(monkeypatch):
    server = MCPServer("detached")

    @server.tool()
    def echo(word: str) -> str:
        return word

    (before,) = await server.list_tools()
    # An SDK where the published output schema no longer reads fn_metadata.
    monkeypatch.setattr(Tool, "output_schema", property(lambda self: {"type": "object"}))
    with pytest.raises(TypeError, match="no longer follows"):
        trim_tool_schemas(server)
    monkeypatch.undo()
    async with Client(server) as client:
        (after,) = (await client.list_tools()).tools
        result = await client.call_tool("echo", {"word": "hi"})
    assert after.output_schema == before.output_schema
    assert result.structured_content == {"result": "hi"}


async def test_trim_dedents_descriptions():
    server = MCPServer("detached")

    # Python 3.11 and 3.12 keep a docstring's source indentation; 3.13 strips it.
    @server.tool(description="Echo a word.\n\n        Second paragraph.\n    ")
    def echo(word: str) -> str:
        return word

    trim_tool_schemas(server)
    async with Client(server) as client:
        (tool,) = (await client.list_tools()).tools
    assert tool.description == "Echo a word.\n\nSecond paragraph."
