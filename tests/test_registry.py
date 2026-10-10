"""Tool group registry: availability, opt-in and disabling, per-tool removal, group settings,
plugins, and the group lifecycle."""

from __future__ import annotations

import importlib.metadata
import logging
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import build_server, host_backend, sandbox_flags, text
from mcp import Client
from mcp.server.mcpserver import MCPServer

import dev_link_mcp.app as app_module
import dev_link_mcp.tools as tools
from dev_link_mcp.cli import main as cli_main
from dev_link_mcp.config import load_config
from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools import ENTRY_POINT_GROUP, Builtin, ToolGroupError, Toolset, register_groups
from dev_link_mcp.tools.base import ToolGroup

REPO = Path(__file__).resolve().parents[1]
HELLO_SRC = REPO / "examples" / "plugin-hello" / "src"

Register = Callable[..., Any]


@pytest.fixture
async def register(tmp_path: Path, workspace: Path) -> AsyncIterator[Register]:
    """`mcp, rt, toolset = await register("<config yaml>", environ={...})` registers every group.

    The YAML goes through the real loader, so errors carry `[from config file <path>]`.
    """
    made: list[tuple[Runtime, list[Toolset]]] = []

    async def _register(body: str = "", environ: dict[str, str] | None = None) -> tuple[MCPServer, Runtime, Toolset]:
        path = tmp_path / "c.yaml"
        path.write_text(body)
        cli = {"workspace": str(workspace), "state_dir": tmp_path / "s", "sandbox": {"backend": host_backend()}}
        cfg = load_config(path, cli, environ=environ or {}, user_file=None)
        rt = Runtime.create(cfg)
        toolsets: list[Toolset] = []
        made.append((rt, toolsets))
        mcp = MCPServer("dev-link-test")
        toolset = await register_groups(mcp, rt)
        toolsets.append(toolset)
        return mcp, rt, toolset

    yield _register
    for rt, toolsets in made:
        for toolset in toolsets:
            await toolset.shutdown()
        rt.audit.close()


async def tool_names(mcp: MCPServer) -> set[str]:
    return {t.name for t in await mcp.list_tools()}


# --- enabling and disabling groups -----------------------------------------------------


async def test_default_groups(register):
    mcp, _rt, toolset = await register()
    assert list(toolset.groups) == [b.name for b in tools.BUILTINS if b.name != "docker"]
    assert toolset.disabled == ["docker"]
    assert toolset.unavailable == {}
    assert set().union(*toolset.tools.values()) == await tool_names(mcp)
    assert "workspace_info" in toolset.tools["system"]


async def test_tools_doc_names_every_tool_of_a_full_listing(register):
    # test_generated_docs_are_current checks that docs/tools.md matches the generator; this checks
    # the generator against the real registry, with every group on.
    mcp, _rt, toolset = await register("tools:\n  enabled: [docker]\n")
    assert list(toolset.groups) == [b.name for b in tools.BUILTINS]
    doc = (REPO / "docs" / "tools.md").read_text()
    assert [name for name in sorted(await tool_names(mcp)) if f"### `{name}`" not in doc] == []
    assert [b.name for b in tools.BUILTINS if f"## {b.name}\n" not in doc] == []


async def test_opt_in_group_needs_enabled_and_disabled_wins(register):
    mcp, _rt, toolset = await register("tools:\n  enabled: [docker]\n  disabled: [http, browser]\n")
    assert "docker" in toolset.groups and "docker_ps" in await tool_names(mcp)
    assert "http" not in toolset.groups and "browser" not in toolset.groups
    assert toolset.disabled == ["http", "browser"]
    assert not {"http_request", "fetch_url"} & await tool_names(mcp)
    assert not any(name.startswith("browser_") for name in await tool_names(mcp))
    _mcp, _rt, toolset = await register("tools:\n  enabled: [docker]\n  disabled: [docker]\n")
    assert "docker" not in toolset.groups and toolset.disabled == ["docker"]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("tools:\n  enabled: [dokcer]\n", r"^tools\.enabled: unknown tool group; known groups: browser, checkpoints,"),
        ("tools:\n  disabled: [gti]\n", r"^tools\.disabled: unknown tool group; known groups: .*\(got 'gti'\)"),
        ("groups:\n  dokcer:\n    x: 1\n", r"^groups\.dokcer: unknown tool group; known groups: .*docker"),
    ],
)
async def test_unknown_group_names_are_errors(register, tmp_path, body, match):
    with pytest.raises(ToolGroupError, match=match) as info:
        await register(body)
    assert str(info.value).endswith(f"[from config file {tmp_path / 'c.yaml'}]")


async def test_missing_requirement_makes_a_group_unavailable_without_importing_it(
    register, tmp_path, monkeypatch, caplog
):
    (tmp_path / "explodes_on_import.py").write_text("raise RuntimeError('must not be imported')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    ghost = Builtin("ghost", "explodes_on_import:Ghost", ("no_such_requirement_xyz",), ("ghost_tool",))
    monkeypatch.setattr(tools, "BUILTINS", (*tools.BUILTINS, ghost))
    hint = "pip install 'dev-link-mcp[ghost]'"
    with caplog.at_level(logging.INFO, logger="dev_link_mcp.tools"):
        # Its tool names may be listed in tools.disabled_tools, so one config works on every machine.
        mcp, rt, toolset = await register("tools:\n  disabled_tools: [ghost_tool]\n")
    assert "explodes_on_import" not in sys.modules
    assert toolset.unavailable == {"ghost": hint} and rt.unavailable_groups == {"ghost": hint}
    assert "ghost" not in toolset.groups and "ghost" not in toolset.tools
    # Only the registry's own log: without bwrap the `none` sandbox backend logs a warning too.
    assert [r.getMessage() for r in caplog.records if r.name == "dev_link_mcp.tools"] == [
        f"tool group ghost is unavailable (no_such_requirement_xyz not installed); install it with: {hint}"
    ]
    async with Client(mcp) as client:
        info = text(await client.call_tool("workspace_info", {}))
    assert f"ghost ({hint})" in info


def test_requirements_match_the_extras_and_tool_lists():
    # Groups with requirements list their tools for machines where they are unavailable; keep that in sync.
    import tomllib

    extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    for b in tools.BUILTINS:
        if b.requires:
            assert b.name in extras, b.name
            assert b.tools, b.name


async def test_builtin_tool_lists_match_registered_tools(rt):
    for b in tools.BUILTINS:
        if b.tools:
            mcp, groups = build_server(rt, b.name)
            assert set(b.tools) == await tool_names(mcp), b.name
            for group in groups:
                await group.shutdown()


# --- tools.disabled_tools ----------------------------------------------------------------


async def test_disabled_tools_removes_exactly_those_tools(register):
    full, _rt, _ = await register()
    mcp, _rt, toolset = await register("tools:\n  disabled_tools: [run_command, fetch_url, run_command]\n")
    assert await tool_names(full) - await tool_names(mcp) == {"run_command", "fetch_url"}
    assert "run_command" not in toolset.tools["shell"] and "fetch_url" not in toolset.tools["http"]
    assert "http_request" in toolset.tools["http"]


async def test_unknown_disabled_tool_lists_close_names(register, tmp_path):
    with pytest.raises(ToolGroupError) as info:
        await register("tools:\n  disabled_tools: [run_comand, zzzz]\n")
    lines = str(info.value).splitlines()
    source = f"[from config file {tmp_path / 'c.yaml'}]"
    assert lines[0].startswith("tools.disabled_tools: no such tool; closest names: run_command")
    assert lines[0].endswith(f"(got 'run_comand') {source}")
    assert lines[1] == f"tools.disabled_tools: no such tool (got 'zzzz') {source}"


async def test_disabled_tools_of_turned_off_groups_are_ignored(register):
    # docker is opt-in and not enabled; http is disabled outright.
    mcp, _rt, toolset = await register(
        "tools:\n  disabled: [http]\n  disabled_tools: [docker_ps, http_request, fetch_url]\n"
    )
    assert "http" not in toolset.groups and "docker" not in toolset.groups
    assert not {"docker_ps", "http_request", "fetch_url"} & await tool_names(mcp)


# --- group settings ----------------------------------------------------------------------


async def test_group_settings_reach_the_group(register):
    body = (
        "tools:\n  enabled: [docker]\n"
        "groups:\n"
        "  browser:\n    headless: false\n    max_pages: 2\n    default_timeout_s: 5\n"
        "  http:\n    max_redirects: 3\n"
        "  project:\n    long_timeout_s: 120\n"
        "  docker:\n    build_timeout_s: 90\n"
    )
    mcp, _rt, toolset = await register(body)
    browser = toolset.groups["browser"]
    assert browser.settings.max_pages == 2 and browser.host.headless is False  # type: ignore[attr-defined]
    listed = {t.name: t for t in await mcp.list_tools()}
    assert listed["browser_open"].input_schema["properties"]["timeout_s"]["default"] == 5.0
    assert "(up to 3)" in listed["http_request"].input_schema["properties"]["follow_redirects"]["description"]
    assert listed["run_tests"].input_schema["properties"]["timeout_s"]["default"] == 120.0
    assert "Default 90" in listed["docker_build"].input_schema["properties"]["timeout_s"]["description"]
    async with Client(mcp) as client:
        pages = await client.call_tool("browser_list_pages", {})
    assert pages.structured_content == {"pages": [], "max_pages": 2}


@pytest.mark.parametrize(
    ("body", "line"),
    [
        (
            "groups:\n  browser:\n    headless: 'no'\n",
            "groups.browser.headless: Input should be a valid boolean (got 'no')",
        ),
        ("groups:\n  http:\n    max_redirect: 3\n", "unknown config key: groups.http.max_redirect"),
        ("groups:\n  files:\n    anything: 1\n", "unknown config key: groups.files.anything"),
        (
            "groups:\n  browser:\n    max_pages: 0\n",
            "groups.browser.max_pages: Input should be greater than or equal to 1",
        ),
    ],
)
async def test_bad_group_settings_use_the_config_error_format(register, tmp_path, body, line):
    with pytest.raises(ToolGroupError) as info:
        await register(body)
    assert str(info.value).startswith(line)
    assert str(info.value).endswith(f"[from config file {tmp_path / 'c.yaml'}]")


async def test_settings_of_turned_off_groups_are_not_checked(register):
    _mcp, _rt, toolset = await register("tools:\n  disabled: [browser]\ngroups:\n  browser:\n    bogus: 1\n")
    assert "browser" not in toolset.groups


async def test_group_settings_from_the_environment(register):
    _mcp, rt, toolset = await register(environ={"DEV_LINK_GROUPS__HTTP__MAX_REDIRECTS": "3"})
    assert toolset.groups["http"].settings.max_redirects == 3  # type: ignore[attr-defined]
    assert rt.config.sources["groups.http.max_redirects"] == "env DEV_LINK_GROUPS__HTTP__MAX_REDIRECTS"
    name = "DEV_LINK_GROUPS__BROWSER__MAX_PAGES"
    with pytest.raises(ToolGroupError) as info:
        await register(environ={name: "many"})
    assert (
        str(info.value) == f"groups.browser.max_pages: Input should be a valid integer (got 'many') [from env {name}]"
    )


# --- plugins -----------------------------------------------------------------------------


@pytest.fixture
def plugins(monkeypatch) -> Callable[..., None]:
    """`plugins(hello="dev_link_hello:HelloGroup")` makes those entry points look installed."""
    monkeypatch.syspath_prepend(str(HELLO_SRC))
    monkeypatch.delitem(sys.modules, "dev_link_hello", raising=False)

    def install(**entries: str) -> None:
        points = [importlib.metadata.EntryPoint(name, value, ENTRY_POINT_GROUP) for name, value in entries.items()]

        def entry_points(**params: Any) -> list[importlib.metadata.EntryPoint]:
            return points if params.get("group") == ENTRY_POINT_GROUP else []

        monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)

    return install


async def test_failed_schema_trim_does_not_stop_the_server(register, monkeypatch, caplog):
    def broken(mcp: MCPServer) -> None:
        raise AttributeError("'MCPServer' object has no attribute '_tool_manager'")

    monkeypatch.setattr(tools, "trim_tool_schemas", broken)
    mcp, _rt, toolset = await register()
    assert "list_dir" in toolset.tools["files"]
    async with Client(mcp) as client:
        result = await client.call_tool("list_dir", {})
    assert not result.is_error
    assert "could not trim tool schemas (mcp " in caplog.text
    assert "AttributeError: 'MCPServer' object has no attribute '_tool_manager'" in caplog.text


async def test_installed_plugin_is_not_loaded_unless_enabled(register, plugins):
    plugins(hello="dev_link_hello:HelloGroup")
    mcp, _rt, toolset = await register()
    assert "hello" not in toolset.groups and "hello" not in await tool_names(mcp)
    assert "dev_link_hello" not in sys.modules


async def test_enabled_plugin_is_registered_with_its_settings(register, plugins):
    plugins(hello="dev_link_hello:HelloGroup")
    mcp, _rt, toolset = await register("tools:\n  enabled: [hello]\ngroups:\n  hello:\n    greeting: Hi\n")
    assert toolset.tools["hello"] == ["hello"]
    async with Client(mcp) as client:
        result = await client.call_tool("hello", {"name": "Ada"})
        hello = next(t for t in (await client.list_tools()).tools if t.name == "hello")
    assert text(result) == "Hi, Ada!"
    # Plugin tools get the same schema trim as built-ins, even after the registry listed them.
    assert result.structured_content is None and hello.output_schema is None
    assert hello.input_schema == {"properties": {"name": {"type": "string"}}, "required": ["name"], "type": "object"}
    mcp, _rt, _ = await register("tools:\n  enabled: [hello]\n")
    async with Client(mcp) as client:
        assert text(await client.call_tool("hello", {"name": "Ada"})) == "Hello, Ada!"
    with pytest.raises(ToolGroupError, match=r"^unknown config key: groups\.hello\.greting \[from config file"):
        await register("tools:\n  enabled: [hello]\ngroups:\n  hello:\n    greting: Hi\n")


async def test_plugin_disabled_and_settings_for_installed_plugin(register, plugins):
    plugins(hello="dev_link_hello:HelloGroup")
    _mcp, _rt, toolset = await register("tools:\n  enabled: [hello]\n  disabled: [hello]\n")
    assert "hello" not in toolset.groups and toolset.disabled == ["docker", "hello"]
    # Settings for an installed but not enabled plugin are allowed and not checked.
    _mcp, _rt, toolset = await register("groups:\n  hello:\n    whatever: 1\n")
    assert "hello" not in toolset.groups


@pytest.mark.parametrize(
    ("entries", "body", "match"),
    [
        (
            {"hello": "dev_link_hello:HelloGroup.Settings"},
            "tools:\n  enabled: [hello]\n",
            r"plugin tool group 'hello' \(dev_link_hello:HelloGroup.Settings\) is not a ToolGroup subclass",
        ),
        (
            {"hello": "no_such_plugin_module:Group"},
            "tools:\n  enabled: [hello]\n",
            r"plugin tool group 'hello' \(no_such_plugin_module:Group\) failed to import: ModuleNotFoundError",
        ),
    ],
)
async def test_broken_plugins_stop_the_start(register, plugins, entries, body, match):
    plugins(**entries)
    with pytest.raises(ToolGroupError, match=match):
        await register(body)


async def test_plugin_named_like_a_builtin_is_ignored(register, plugins, caplog):
    plugins(http="dev_link_hello:HelloGroup")
    mcp, _rt, toolset = await register("tools:\n  enabled: [http]\n")
    assert "http_request" in toolset.tools["http"] and "hello" not in await tool_names(mcp)
    assert "dev_link_hello" not in sys.modules
    assert "ignoring plugin tool group 'http'" in caplog.text


async def test_plugin_installed_twice_only_fails_when_enabled(register, monkeypatch):
    points = [
        importlib.metadata.EntryPoint("hello", "pkg_a:Group", ENTRY_POINT_GROUP),
        importlib.metadata.EntryPoint("hello", "pkg_b:Group", ENTRY_POINT_GROUP),
    ]
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda **p: points if p.get("group") == ENTRY_POINT_GROUP else []
    )
    _mcp, _rt, toolset = await register()
    assert "hello" not in toolset.groups
    with pytest.raises(ToolGroupError, match=r"'hello' is installed by more than one package: pkg_a:Group and pkg_b"):
        await register("tools:\n  enabled: [hello]\n")


# --- lifecycle ---------------------------------------------------------------------------


class _Recorder(ToolGroup):
    stopped: ClassVar[list[str]] = []

    def register(self, mcp: MCPServer) -> None:
        tool_name = f"{self.name}_ping"

        @mcp.tool(name=tool_name)
        def ping() -> str:
            return "pong"

    async def shutdown(self) -> None:
        _Recorder.stopped.append(self.name)


class RecorderA(_Recorder):
    name = "rec_a"


class RecorderB(_Recorder):
    name = "rec_b"


class FailingShutdown(_Recorder):
    name = "boom"

    async def shutdown(self) -> None:
        _Recorder.stopped.append(self.name)
        raise RuntimeError("shutdown failed")


@pytest.fixture
def recorders(monkeypatch) -> list[str]:
    """Replace the built-ins with system plus three groups that record their shutdown."""
    monkeypatch.setattr(_Recorder, "stopped", [])
    monkeypatch.setattr(
        tools,
        "BUILTINS",
        (
            Builtin("system", "dev_link_mcp.tools.system:SystemGroup"),
            Builtin("rec_a", f"{__name__}:RecorderA"),
            Builtin("boom", f"{__name__}:FailingShutdown"),
            Builtin("rec_b", f"{__name__}:RecorderB"),
        ),
    )
    return _Recorder.stopped


@pytest.mark.parametrize("fails", [False, True])
async def test_server_shutdown_calls_shutdown_on_every_group(
    tmp_path, workspace, monkeypatch, recorders, caplog, fails
):
    class FakeUvicorn:
        abort_when = None
        aborted = None

        def __init__(self, config: Any) -> None:
            pass

        async def serve(self) -> None:
            assert recorders == []
            if fails:
                raise RuntimeError("server crashed")

    monkeypatch.setattr(app_module, "_Server", FakeUvicorn)
    cli = {"workspace": str(workspace), "state_dir": tmp_path / "s", "sandbox": {"backend": host_backend()}}
    cfg = load_config(None, cli, environ={}, user_file=None)
    if fails:
        with pytest.raises(RuntimeError, match="server crashed"):
            await app_module.serve(cfg)
    else:
        await app_module.serve(cfg)
    assert recorders == ["rec_a", "boom", "rec_b"]
    assert "shutdown of tool group boom failed" in caplog.text


async def test_groups_are_shut_down_when_registration_fails(register, recorders):
    with pytest.raises(ToolGroupError, match="no such tool"):
        await register("tools:\n  disabled_tools: [nope]\n")
    assert recorders == ["rec_a", "boom", "rec_b"]


def test_cli_reports_group_errors_and_exits_2(tmp_path, workspace, capsys):
    with pytest.raises(SystemExit) as info:
        cli_main(["serve", str(workspace), "--state-dir", str(tmp_path / "s"), "--enable", "dokcer", *sandbox_flags()])
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("dev-link: tools.enabled: unknown tool group; known groups: browser,")
    assert err.rstrip().endswith("(got 'dokcer') [from cli --enable]")


# --- browser state is per server ---------------------------------------------------------


@pytest.mark.browser
async def test_each_browser_group_has_its_own_host(rt):
    mcp_a, (group_a,) = build_server(rt, "browser")
    mcp_b, (group_b,) = build_server(rt, "browser")
    assert group_a.host is not group_b.host  # type: ignore[attr-defined]
    try:
        async with Client(mcp_a) as a, Client(mcp_b) as b:
            opened = await a.call_tool("browser_open", {"url": "about:blank"})
            assert not opened.is_error, text(opened)
            assert opened.structured_content["page_id"] == "p1"
            assert (await b.call_tool("browser_list_pages", {})).structured_content["pages"] == []
            # Page ids are numbered per server, so the second server starts at p1 too.
            assert (await b.call_tool("browser_open", {"url": "about:blank"})).structured_content["page_id"] == "p1"
            await group_b.shutdown()
            pages = (await a.call_tool("browser_list_pages", {})).structured_content["pages"]
            assert [p["page_id"] for p in pages] == ["p1"]
    finally:
        await group_a.shutdown()
        await group_b.shutdown()
