import asyncio
import signal
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import httpx2
import mcp.types as types
import pytest
import uvicorn
from conftest import host_backend
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

import dev_link_mcp.middleware as middleware
import dev_link_mcp.tools as tools
from dev_link_mcp.app import _banner, _Server, build, connection_info
from dev_link_mcp.audit import AuditStore
from dev_link_mcp.config import Config
from dev_link_mcp.middleware import AuditMiddleware, result_text
from dev_link_mcp.runtime import Runtime

# --- middleware.result_text -------------------------------------------------------------


def test_result_text_text_blocks():
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="a"), types.TextContent(type="text", text="b")]
    )
    assert result_text(result) == (False, "a\nb")


def test_result_text_error_and_image():
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="bad"),
            types.ImageContent(type="image", data="AAAA", mime_type="image/png"),
        ],
        is_error=True,
    )
    assert result_text(result) == (True, "bad\n[image block]")


def test_result_text_structured_only():
    result = types.CallToolResult(content=[], structured_content={"x": 1})
    is_error, text = result_text(result)
    assert not is_error and '"x": 1' in text


def test_result_text_dict_none_and_model():
    assert result_text({"isError": True, "content": []})[0] is True
    sdk_error = {"content": [{"type": "text", "text": "Unknown tool: nope"}], "isError": True, "_meta": {"k": 1}}
    assert result_text(sdk_error) == (True, "Unknown tool: nope")
    assert result_text({"is_error": False, "a": 1}) == (False, '{"is_error": false, "a": 1}')
    assert result_text(None) == (False, "")

    class Other(BaseModel):
        value: int = 3

    assert result_text(Other()) == (False, '{"value":3}')  # type: ignore[arg-type]


# --- middleware.AuditMiddleware -----------------------------------------------------------


async def test_client_labels_are_capped_oldest_first(tmp_path, monkeypatch):
    # Sessions are never announced as closed, so one label per session must not pile up forever.
    monkeypatch.setattr(middleware, "_MAX_CLIENTS", 3)
    store = AuditStore(tmp_path, secrets=[], result_bytes=1000)
    mw = AuditMiddleware(store)

    async def call_next(ctx):
        return None

    def handshake(session: str) -> SimpleNamespace:
        return SimpleNamespace(
            method="initialize",
            params={"clientInfo": {"name": f"client-{session}", "version": "1"}},
            request=SimpleNamespace(headers={"mcp-session-id": session}),
            session=None,
            protocol_version="2025-06-18",
        )

    try:
        for session in ("a", "b", "c", "a", "d"):
            await mw(handshake(session), call_next)  # type: ignore[arg-type]
    finally:
        store.close()
    # "a" shook hands again, so "b" was the oldest when "d" arrived.
    assert list(mw._clients) == ["c", "a", "d"]
    assert mw._clients["a"] == "client-a 1"


# --- end-to-end over real HTTP ----------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def running_server(tmp_path: Path, workspace: Path, auth: str) -> AsyncIterator[tuple[Config, Runtime]]:
    cfg = Config(
        workspace=workspace,
        state_dir=tmp_path / f"state-{auth}",
        server={"port": _free_port(), "auth": auth},
        sandbox={"backend": host_backend()},
    ).finalize()
    app, rt, toolset = await build(cfg)
    assert list(toolset.tools) == ["system"]
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=cfg.server.port, log_level="warning", access_log=False, lifespan="on"
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.02)
        yield cfg, rt
    finally:
        server.should_exit = True
        await task
        await rt.processes.shutdown()
        rt.audit.close()


async def test_server_main_loop_ticks_once_a_second_and_wakes_on_signal():
    # uvicorn's own loop wakes every 0.1 s; an idle dev-link server should not.
    server = _Server(uvicorn.Config(app=None))
    ticks: list[int] = []

    async def on_tick(counter: int) -> bool:
        ticks.append(counter)
        return server.should_exit

    server.on_tick = on_tick  # type: ignore[method-assign]
    task = asyncio.create_task(server.main_loop())
    await asyncio.sleep(1.5)
    # Every tick refreshes the Date header, which uvicorn does when counter % 10 == 0.
    assert 1 <= len(ticks) <= 3 and all(c % 10 == 0 for c in ticks), ticks
    started = time.monotonic()
    server.handle_exit(signal.SIGTERM, None)
    await asyncio.wait_for(task, 0.5)
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("auth", ["path-token", "bearer"])
def test_banner_shows_the_token_only_on_a_terminal(tmp_path, workspace, auth):
    cfg = Config(workspace=workspace, state_dir=tmp_path / "state", server={"auth": auth}).finalize()
    token = cfg.server.token
    conn_file = cfg.state_dir / "connection.json"
    shown = _banner(cfg, "bubblewrap", 12, conn_file, show_token=True)
    # The dashboard URL and the `claude mcp add` line, plus the MCP URL in path-token mode.
    assert shown.count(token) == (3 if auth == "path-token" else 2)
    assert "***" not in shown and "connection.json" not in shown
    hidden = _banner(cfg, "bubblewrap", 12, conn_file, show_token=False)
    assert token not in hidden
    assert hidden.count("***") == shown.count(token)
    assert f"  token     : hidden because this output is not a terminal; see {conn_file}\n" in hidden


def test_banner_without_auth_has_nothing_to_hide(tmp_path, workspace):
    cfg = Config(workspace=workspace, state_dir=tmp_path / "state", server={"auth": "none", "token": "e"}).finalize()
    conn_file = cfg.state_dir / "connection.json"
    shown = _banner(cfg, "bubblewrap", 12, conn_file, show_token=True)
    assert _banner(cfg, "bubblewrap", 12, conn_file, show_token=False) == shown
    assert "token" not in shown and "***" not in shown
    assert "  workspace : " in shown and "  sandbox   : bubblewrap\n" in shown


def test_banner_lines_up_the_public_urls(tmp_path, workspace):
    cfg = Config(workspace=workspace, state_dir=tmp_path / "state", server={"public_url": "https://pub.example"})
    shown = _banner(cfg.finalize(), "bubblewrap", 12, tmp_path / "connection.json", show_token=False)
    labels = ("workspace", "MCP URL", "dashboard", "public MCP URL", "public dashboard", "token", "logs")
    fields = [line for line in shown.splitlines() if line.strip().partition(":")[0].strip() in labels]
    assert len(fields) == len(labels), shown
    assert len({line.index(":") for line in fields}) == 1, shown


@pytest.fixture(autouse=True)
def _only_system_tools(monkeypatch):
    monkeypatch.setattr(tools, "BUILTINS", tuple(b for b in tools.BUILTINS if b.name == "system"))


async def _exercise(client: Client) -> None:
    names = {t.name for t in (await client.list_tools()).tools}
    assert {"workspace_info", "server_status"} <= names
    result = await client.call_tool("server_status", {})
    assert not result.is_error
    result = await client.call_tool("no_such_tool", {})
    assert result.is_error


def _assert_token_not_logged(cfg: Config) -> None:
    assert cfg.server.token
    for name in ("audit.jsonl", "audit.sqlite3", "audit.sqlite3-wal"):
        path = cfg.state_dir / name
        if path.exists():
            assert cfg.server.token.encode() not in path.read_bytes(), name


async def test_path_token_mode(tmp_path, workspace):
    async with running_server(tmp_path, workspace, "path-token") as (cfg, _rt):
        info = connection_info(cfg)
        assert info["mcp_url"].endswith(f"/mcp/{cfg.server.token}") and "headers" not in info
        async with Client(info["mcp_url"]) as client:
            await _exercise(client)
        base = cfg.base_url()
        async with httpx.AsyncClient() as http:
            assert (await http.post(base + "/mcp/wrongtokenwrongtoken", json={})).status_code == 404
            assert (await http.post(base + "/mcp", json={})).status_code == 404
            assert (await http.get(base + "/ui/wrongtokenwrongtoken/")).status_code == 404
            assert (await http.get(base + "/ui/")).status_code == 404
            health = await http.get(base + "/health")
            assert health.status_code == 200 and health.json() == {"status": "ok"}
            dashboard = await http.get(base + cfg.ui_path + "/")
            assert dashboard.status_code == 200 and 'src="js/main.js"' in dashboard.text
            events = (await http.get(base + cfg.ui_path + "/api/events")).json()
            stats = (await http.get(base + cfg.ui_path + "/api/stats")).json()
        tool_events = [e for e in events if e["kind"] == "tool"]
        assert {e["name"] for e in tool_events} == {"server_status", "no_such_tool"}
        assert {e["name"]: e["status"] for e in tool_events} == {"server_status": "ok", "no_such_tool": "error"}
        assert any(e["kind"] == "session" for e in events)
        assert stats["calls"] == 2 and stats["errors"] == 1
    _assert_token_not_logged(cfg)


async def test_bearer_mode(tmp_path, workspace):
    async with running_server(tmp_path, workspace, "bearer") as (cfg, _rt):
        info = connection_info(cfg)
        assert info["mcp_url"].endswith("/mcp") and info["headers"] == {"Authorization": f"Bearer {cfg.server.token}"}
        base = cfg.base_url()
        async with httpx.AsyncClient() as http:
            missing = await http.post(base + "/mcp", json={})
            assert missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"
            wrong = await http.post(base + "/mcp", json={}, headers={"Authorization": "Bearer " + "x" * 32})
            assert wrong.status_code == 401
            assert (await http.post(base + "/mcp/", json={})).status_code in (401, 404)
            assert (await http.get(base + "/health")).status_code == 200
            assert (await http.get(base + cfg.ui_path + "/api/stats")).status_code == 200
            assert (await http.get(base + "/ui/")).status_code == 404
        async with (
            httpx2.AsyncClient(headers=info["headers"]) as http2,
            Client(streamable_http_client(info["mcp_url"], http_client=http2)) as client,
        ):
            await _exercise(client)
    _assert_token_not_logged(cfg)


async def test_no_auth_mode(tmp_path, workspace):
    async with running_server(tmp_path, workspace, "none") as (cfg, _rt):
        assert cfg.server.token == ""
        info = connection_info(cfg)
        assert info["mcp_url"] == cfg.base_url() + "/mcp"
        async with Client(info["mcp_url"]) as client:
            await _exercise(client)
        async with httpx.AsyncClient() as http:
            assert (await http.get(cfg.base_url() + "/ui/")).status_code == 200
            assert (await http.get(cfg.base_url() + "/health")).status_code == 200


_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}},
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def test_no_auth_mode_blocks_dns_rebinding_and_cross_site_requests(tmp_path, workspace):
    # Without a secret in the URL, Host and Origin checks are the only thing stopping a web page
    # the user visits from driving the server.
    async with running_server(tmp_path, workspace, "none") as (cfg, _rt):
        base = cfg.base_url()
        async with httpx.AsyncClient() as http:
            evil_host = {"Host": "evil.example"}
            evil_origin = {"Origin": "http://evil.example"}
            assert (
                await http.post(base + "/mcp", json=_INIT, headers={**_MCP_HEADERS, **evil_host})
            ).status_code == 421
            assert (
                await http.post(base + "/mcp", json=_INIT, headers={**_MCP_HEADERS, **evil_origin})
            ).status_code == 403
            assert (await http.get(base + "/ui/", headers=evil_host)).status_code == 421
            assert (await http.get(base + "/ui/api/events", headers=evil_host)).status_code == 421
            assert (await http.post(base + "/ui/api/processes/p1/stop", headers=evil_origin)).status_code == 403
            # Same-origin requests still work.
            own_origin = {"Origin": base}
            assert (await http.get(base + "/ui/api/events", headers=own_origin)).status_code == 200
            assert (await http.post(base + "/ui/api/processes/p1/stop", headers=own_origin)).status_code == 404


async def test_path_token_dashboard_api_needs_the_token(tmp_path, workspace):
    async with running_server(tmp_path, workspace, "path-token") as (cfg, _rt):
        base = cfg.base_url()
        async with httpx.AsyncClient() as http:
            for path in ("/ui/api/events", "/ui/x" * 1 + "/api/stats", f"/ui/{cfg.server.token[:-1]}/api/events"):
                assert (await http.get(base + path)).status_code == 404, path
            assert (await http.post(base + "/ui/api/processes/p1/stop")).status_code == 404
            assert (await http.get(base + cfg.ui_path + "/api/events")).status_code == 200
