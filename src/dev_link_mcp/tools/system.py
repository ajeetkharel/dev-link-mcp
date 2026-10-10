"""Workspace and server information."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import READ_ONLY

_TOOLCHAINS = {
    "python3": "python3 --version",
    "uv": "uv --version",
    "node": "node --version",
    "npm": "npm --version",
    "pnpm": "pnpm --version",
    "yarn": "yarn --version",
    "bun": "bun --version",
    "deno": "deno --version",
    "java": "java -version",
    "mvn": "mvn -v",
    "gradle": "gradle --version",
    "go": "go version",
    "cargo": "cargo --version",
    "rustc": "rustc --version",
    "gcc": "gcc --version",
    "make": "make --version",
    "cmake": "cmake --version",
    "git": "git --version",
    "rg": "rg --version",
    "dotnet": "dotnet --version",
    "php": "php --version",
    "ruby": "ruby --version",
}


def _register(mcp: MCPServer, rt: Runtime) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def workspace_info() -> str:
        """Describe the environment: workspace root, sandbox mode and network access, limits, and
        installed toolchains with versions. Call this first in a new session."""
        cfg = rt.config
        probe = "; ".join(
            f'command -v {name} >/dev/null && echo "{name}: $({cmd} 2>&1 | grep -v "^\\s*$" | head -1)"'
            for name, cmd in _TOOLCHAINS.items()
        )
        result = await rt.sandbox.run(rt.sandbox.shell_argv(probe), timeout_s=60)
        if rt.sandbox.enabled:
            network = "allowed" if cfg.sandbox.network else "blocked"
            mode = [
                f"sandbox: {rt.sandbox.backend.name}; network {network}",
                *(
                    []
                    if cfg.sandbox.network
                    else [
                        "With network blocked each command has its own network namespace, so a server started"
                        " with start_process cannot be reached from other commands or tools: the wait_for_port"
                        " option of start_process and the port option of wait_for_process are refused, and"
                        " http_request, fetch_url and download_file refuse every URL. The browser only opens"
                        " about:blank, its pages can load only data: and blob: URLs, and WebRTC is turned off."
                    ]
                ),
                "Anything goes inside the workspace. Outside it, system dirs are read-only and the rest"
                " of the host is hidden. HOME is a private directory that persists between calls"
                " (package caches, tool config). /tmp is shared by all commands until the server restarts.",
            ]
        else:
            mode = [
                f"sandbox: {rt.sandbox.backend.name} (OFF, full local access)",
                "Commands run as the user with their real HOME and environment and can reach the whole"
                " machine. File tools accept any host path too.",
            ]
        lines = [
            f"workspace: {rt.workspace.root}",
            "File paths are relative to the workspace root; absolute paths must stay inside it.",
            *mode,
            "Use start_process for servers and watchers so they are tracked and logged (in the sandbox,"
            " background jobs started by run_command die when it returns). Work on the current branch or"
            " in a git worktree, whichever suits the task.",
            f"limits: default timeout {cfg.limits.default_timeout_s:.0f}s, max {cfg.limits.max_timeout_s:.0f}s,"
            f" output cap {cfg.limits.max_output_bytes} bytes, max background processes {cfg.limits.max_processes}"
            + (f", memory per command {cfg.limits.memory_mb} MiB" if cfg.limits.memory_mb is not None else "")
            + (f", processes and threads per command {cfg.limits.max_pids}" if cfg.limits.max_pids is not None else ""),
            *(
                [
                    "tool groups not installed on this server (ask the user to install them): "
                    + ", ".join(f"{name} ({hint})" for name, hint in rt.unavailable_groups.items())
                ]
                if rt.unavailable_groups
                else []
            ),
            "",
            "toolchains:",
            result.stdout.strip() or "(none detected)",
        ]
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    async def server_status() -> dict[str, Any]:
        """Server health: background processes, tool calls and errors in the last 24 hours, and
        free disk space on the workspace volume."""
        stats = await asyncio.to_thread(rt.audit.stats)
        disk = shutil.disk_usage(rt.workspace.root)
        return {
            "processes": [{**p, "cwd": rt.workspace.rel(Path(p["cwd"]))} for p in rt.processes.list()],
            "calls_24h": stats["calls"],
            "errors_24h": stats["errors"],
            "disk_free_gb": round(disk.free / 1e9, 2),
        }


class SystemGroup(ToolGroup):
    """Workspace and server information."""

    name = "system"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)
