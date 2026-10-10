"""Server assembly: MCP tools, auth, the dashboard and the HTTP server loop."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from dev_link_mcp import __version__
from dev_link_mcp.audit import redact
from dev_link_mcp.config import LOOPBACK_HOSTS, Config
from dev_link_mcp.middleware import AuditMiddleware
from dev_link_mcp.private import open_private
from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools import Toolset, register_groups
from dev_link_mcp.web import add_dashboard_routes

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
dev-link-mcp is a development environment for one workspace folder. Work as you would in
your own terminal: you may run any command and change anything in the workspace.

- Call workspace_info first: it shows the sandbox mode, network access, limits and
  installed toolchains.
- run_command runs any shell command (cat, sed, rg, python, npm, git, ...); use it for git
  too. The file and search tools are shortcuts with numbered reads, exact-match edits,
  patches, images and compact results. Use whichever fits.
- Use start_process for servers, watchers and anything long-running, and process_output to
  read their output. The browser_* tools load pages in Chromium and save screenshots under
  .dev-link/artifacts.
- Relative paths are resolved from the workspace root. Commands normally run in a bubblewrap
  sandbox where the workspace is writable and the rest of the host is read-only or hidden.
- Work on the current branch or in a git worktree, whichever suits the task.
  checkpoint_create and checkpoint_restore give a cheap undo for risky changes.
Every call is logged and shown to the user on a live dashboard.
"""


class BearerAuth:
    """ASGI guard requiring `Authorization: Bearer <token>` on the MCP endpoint."""

    def __init__(self, app: ASGIApp, token: str, mcp_path: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.mcp_path = mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == self.mcp_path:
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                response = JSONResponse(
                    {"error": "missing or invalid bearer token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def assemble(config: Config) -> tuple[MCPServer, Runtime, Toolset]:
    """Create the runtime and an MCP server with every enabled tool group registered.

    Adds no routes and records nothing in the audit log, so it also serves listings such as
    `dev-link tools`. The caller owns the result: shut the toolset down and close the audit
    store when done. Raises SandboxUnavailable and ToolGroupError like build(); the runtime is
    closed again in that case.
    """
    rt = Runtime.create(config)
    mcp: MCPServer = MCPServer(
        "dev-link-mcp",
        title="dev-link",
        description="Sandboxed development environment for one workspace folder",
        instructions=INSTRUCTIONS,
        version=__version__,
        middleware=[AuditMiddleware(rt.audit)],
        log_level=config.log_level,
    )
    try:
        toolset = await register_groups(mcp, rt)
    except BaseException:
        rt.audit.close()
        raise
    return mcp, rt, toolset


async def build(config: Config) -> tuple[Starlette, Runtime, Toolset]:
    """Create the runtime, register tools and routes. Returns (ASGI app, runtime, tool groups).

    Raises SandboxUnavailable when the sandbox cannot run here, and ToolGroupError for invalid
    tool group settings (see register_groups); the runtime is closed again in that case.
    Records a server start event in the audit log.
    """
    mcp, rt, toolset = await assemble(config)
    add_dashboard_routes(mcp, rt, toolset)

    security: TransportSecuritySettings | None = None
    extra_hosts = list(config.server.allowed_hosts)
    if config.server.public_url:
        extra_hosts.append(urlsplit(config.server.public_url).netloc)
    if config.server.host not in LOOPBACK_HOSTS or extra_hosts:
        # The SDK only builds DNS-rebinding settings itself for a bare loopback bind, and it
        # matches Host exactly, so tunnel and LAN hostnames have to be listed here.
        hosts = extra_hosts or [f"{config.server.host}:{config.server.port}"]
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *hosts],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                *(f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")),
            ],
        )
    app: Starlette = mcp.streamable_http_app(
        streamable_http_path=config.mcp_path, transport_security=security, host=config.server.host
    )
    if config.server.auth == "bearer":
        app.add_middleware(BearerAuth, token=config.server.token, mcp_path=config.mcp_path)
    rt.audit.record(
        "server",
        "start",
        {
            "workspace": str(config.workspace),
            "auth": config.server.auth,
            "sandbox": rt.sandbox.backend.name,
            "tools": toolset.tools,
        },
    )
    return app, rt, toolset


def connection_info(config: Config) -> dict[str, Any]:
    base = config.base_url()
    info: dict[str, Any] = {"mcp_url": base + config.mcp_path, "dashboard_url": base + config.ui_path + "/"}
    if config.server.public_url:
        public = config.server.public_url.rstrip("/")
        info["public_mcp_url"] = public + config.mcp_path
        info["public_dashboard_url"] = public + config.ui_path + "/"
    if config.server.auth == "bearer":
        info["headers"] = {"Authorization": f"Bearer {config.server.token}"}
    return info


def _banner(config: Config, sandbox: str, groups_tools: int, conn_file: Path, *, show_token: bool) -> str:
    """The startup summary. Without `show_token`, every copy of the token is *** and a line
    points to `conn_file`, the connection file, which has it; with auth `none` there is no token."""
    info = connection_info(config)
    add = f"claude mcp add --transport http dev-link {info['mcp_url']}"
    if config.server.auth == "bearer":
        add += f' --header "Authorization: Bearer {config.server.token}"'
    hide = not show_token and bool(config.server.token)
    hidden = [f"  token     : hidden because this output is not a terminal; see {conn_file}"] if hide else []
    text = "\n".join(
        [
            "",
            "  dev-link-mcp ready",
            f"  workspace : {config.workspace}",
            f"  tools     : {groups_tools}",
            f"  auth      : {config.server.auth}",
            f"  sandbox   : {sandbox}",
            f"  MCP URL   : {info['mcp_url']}",
            f"  dashboard : {info['dashboard_url']}",
            *(
                [
                    f"  public MCP URL   : {info['public_mcp_url']}",
                    f"  public dashboard : {info['public_dashboard_url']}",
                ]
                if config.server.public_url
                else []
            ),
            *hidden,
            f"  logs      : {config.state_dir}",
            *(
                ["", "  WARNING: anyone with the public URL can run commands in this workspace."]
                if config.server.public_url
                else []
            ),
            "",
            "  Claude Code:",
            f"    {add}",
            "",
        ]
    )
    return redact(text, [config.server.token]) if hide else text


class ServeAborted(Exception):
    """The server shut itself down because something it depends on failed; the message says what."""


class _Server(uvicorn.Server):
    """uvicorn.Server that sleeps while idle.

    uvicorn checks for shutdown every 0.1 s, so an idle server wakes ten times a second. This one
    ticks once a second, which still refreshes the Date header on time, and wakes at once when a
    signal asks it to stop. Each tick also calls `abort_when`; a reason from it stops the server
    and is kept in `aborted`.
    """

    _loop: asyncio.AbstractEventLoop | None = None
    _wake: asyncio.Event | None = None
    abort_when: Callable[[], str | None] | None = None
    aborted: str | None = None

    async def main_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        counter = 0
        while not await self.on_tick(counter):
            if self.abort_when is not None and (reason := self.abort_when()) is not None:
                log.error("shutting down: %s", reason)
                self.aborted = reason
                self.should_exit = True
                break
            # on_tick refreshes the Date header when counter % 10 == 0, so on every tick here.
            counter += 10
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 1)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        super().handle_exit(sig, frame)
        if self._loop is not None and self._wake is not None:
            # Runs in a signal handler, so hand the wake-up to the loop instead of touching it here.
            self._loop.call_soon_threadsafe(self._wake.set)


async def serve(
    config: Config,
    *,
    on_shutdown: Callable[[], None] | None = None,
    abort_when: Callable[[], str | None] | None = None,
) -> None:
    """Run the server until it is told to stop.

    Writes `state_dir/connection.json` (0600: the URLs plus the workspace and this PID) once the
    tools are registered, and removes it on exit. `on_shutdown` runs first when the server
    stops, before background processes and tool groups are shut down; the CLI stops the tunnel
    there so nothing stays reachable while the rest winds down. `abort_when` is called about
    once a second while the server runs; when it returns a reason, the server shuts down as for
    a signal and then raises ServeAborted with that reason. The CLI uses it to notice a dead
    tunnel. Otherwise raises like build().
    """
    app, rt, toolset = await build(config)
    assert config.state_dir is not None
    conn_file = config.state_dir / "connection.json"
    server: _Server | None = None
    try:
        with os.fdopen(open_private(conn_file), "w") as f:
            json.dump({**connection_info(config), "workspace": str(config.workspace), "pid": os.getpid()}, f, indent=2)
        tool_count = sum(len(names) for names in toolset.tools.values())
        # Output that is not a terminal usually lands in a file (`dev-link start` writes server.log),
        # where the token would outlive the run; on a terminal the banner is how the user gets it.
        banner = _banner(config, rt.sandbox.backend.name, tool_count, conn_file, show_token=sys.stderr.isatty())
        print(banner, file=sys.stderr, flush=True)
        # access_log is off because request lines would contain the path token. SSE streams (an open
        # dashboard tab, an MCP client's GET stream) never end on their own, so without a shutdown
        # timeout uvicorn waits for them forever and Ctrl+C cannot stop the server. log_config=None
        # leaves uvicorn's loggers to the root handlers the CLI sets up, which redact the token.
        server = _Server(
            uvicorn.Config(
                app,
                host=config.server.host,
                port=config.server.port,
                log_level=config.log_level.lower(),
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=3,
            )
        )
        server.abort_when = abort_when
        async with rt.prune_periodically():
            await server.serve()
    finally:
        try:
            if on_shutdown is not None:
                on_shutdown()
        finally:
            conn_file.unlink(missing_ok=True)
            # Groups such as the browser hold processes that must not outlive the server. Both run at
            # once so their grace periods overlap within the 10 s before `dev-link stop` sends SIGKILL.
            await asyncio.gather(rt.processes.shutdown(), toolset.shutdown())
            aborted = server.aborted if server is not None else None
            rt.audit.record("server", "stop", ok=aborted is None, error=aborted)
            rt.audit.close()
    if aborted is not None:
        raise ServeAborted(aborted)
