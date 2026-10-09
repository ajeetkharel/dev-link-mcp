"""Shared fixtures: a Runtime over a temp workspace and an in-process MCP client.

Also the host requirement markers (`bwrap`, `browser`, `docker`, `network`, registered in
pyproject.toml): a marked test is skipped, with the reason, when this host lacks what it needs.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import mcp.types as types
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from dev_link_mcp.config import Config
from dev_link_mcp.middleware import AuditMiddleware
from dev_link_mcp.runtime import Runtime
from dev_link_mcp.sandbox.backends.bubblewrap import Bubblewrap
from dev_link_mcp.tools import BUILTINS, load_group_class, trim_tool_schemas
from dev_link_mcp.tools.base import ToolGroup

DOCKER_IMAGE = "busybox:latest"

# Prints only the first line of a launch error: Playwright appends a multi-line install banner.
_CHROMIUM_LAUNCH = """
import sys
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        p.chromium.launch(headless=True).close()
except Exception as exc:
    sys.exit(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
"""


@functools.cache
def bwrap_unavailable() -> str | None:
    """None when a bubblewrap sandbox works on this host, else why not.

    Uses the backend's own check, which runs a throwaway sandbox: bwrap being installed is not
    enough when AppArmor or a container forbids the namespaces.
    """
    root = Path("/")
    return Bubblewrap(Config(workspace=root), home=root, tmp=root).available()


def host_backend() -> str:
    """The sandbox backend for tests that only need commands to run: `auto`, or `none` without bubblewrap."""
    return "auto" if bwrap_unavailable() is None else "none"


def sandbox_flags() -> list[str]:
    """CLI flags for a server that only needs commands to run: no flags, or `--no-sandbox` without bubblewrap.

    End-to-end runs use the real sandbox where it works, so they cover the default setup.
    """
    return [] if bwrap_unavailable() is None else ["--no-sandbox"]


@functools.cache
def browser_unavailable() -> str | None:
    """None when the `browser` extra is installed and Chromium launches, else why not."""
    if importlib.util.find_spec("playwright") is None:
        return "the browser extra is not installed (uv sync --all-extras)"
    # A child process keeps a hung or crashing Chromium out of the test session and bounds it.
    try:
        done = subprocess.run([sys.executable, "-c", _CHROMIUM_LAUNCH], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "Chromium did not launch within 60 s"
    if done.returncode == 0:
        return None
    lines = done.stderr.strip().splitlines()
    return f"Chromium cannot launch: {lines[-1] if lines else f'exit code {done.returncode}'}"


@functools.cache
def docker_unavailable() -> str | None:
    """None when the docker CLI reaches a daemon that has (or can pull) the test image, else why not."""
    if shutil.which("docker") is None:
        return "docker CLI not installed"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
            return "docker daemon unreachable"
        inspect = ["docker", "image", "inspect", DOCKER_IMAGE]
        if subprocess.run(inspect, capture_output=True, timeout=20).returncode == 0:
            return None
        if subprocess.run(["docker", "pull", DOCKER_IMAGE], capture_output=True, timeout=180).returncode == 0:
            return None
    except (OSError, subprocess.SubprocessError) as exc:
        return f"docker check failed: {exc}"
    return f"docker image {DOCKER_IMAGE} unavailable and could not be pulled"


@functools.cache
def network_unavailable() -> str | None:
    """None when this host can open an outbound TCP connection to the internet, else why not."""
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=3):
            return None
    except OSError as exc:
        return f"host has no outbound network ({exc})"


_REQUIREMENTS = {
    "bwrap": bwrap_unavailable,
    "browser": browser_unavailable,
    "docker": docker_unavailable,
    "network": network_unavailable,
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip marked tests whose requirement this host lacks.

    Each probe runs only when a collected test carries its marker, so running one file that
    needs none of them never launches Chromium or docker.
    """
    for item in items:
        for marker, unavailable in _REQUIREMENTS.items():
            if item.get_closest_marker(marker) is None:
                continue
            reason = unavailable()
            if reason is not None:
                item.add_marker(pytest.mark.skip(reason=f"needs {marker}: {reason}"))


@pytest.fixture(autouse=True)
def _isolated_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own ~/.config/dev-link and DEV_LINK_* variables out of every test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    for name in list(os.environ):
        if name.startswith("DEV_LINK_"):
            monkeypatch.delenv(name)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def config(tmp_path: Path, workspace: Path) -> Config:
    """A finalized config on the default `auto` backend, or `none` where bubblewrap cannot run.

    Most tests only need commands to run, so they also pass on hosts without bubblewrap. Tests
    that rely on its isolation carry the `bwrap` marker and are skipped there.
    """
    return Config(workspace=workspace, state_dir=tmp_path / "state", sandbox={"backend": host_backend()}).finalize()


@pytest.fixture
async def rt(config: Config) -> AsyncIterator[Runtime]:
    runtime = Runtime.create(config)
    yield runtime
    await runtime.processes.shutdown()
    runtime.audit.close()


def build_server(
    rt: Runtime, *names: str, settings: dict[str, dict[str, Any]] | None = None
) -> tuple[MCPServer, list[ToolGroup]]:
    """MCPServer with only the given built-in groups registered, and the group instances.

    Schemas are trimmed with trim_tool_schemas, as register_groups does for the real server.

    `settings` maps a group name to its raw `groups.<name>` values. The caller shuts the groups down.
    """
    mcp: MCPServer = MCPServer("dev-link-test", middleware=[AuditMiddleware(rt.audit)])
    targets = {b.name: b.target for b in BUILTINS}
    groups = []
    for name in names:
        cls = load_group_class(targets[name])
        group = cls(rt, cls.Settings.model_validate((settings or {}).get(name, {})))
        group.register(mcp)
        groups.append(group)
    trim_tool_schemas(mcp)
    return mcp, groups


@pytest.fixture
def connect(rt: Runtime) -> Callable[..., Any]:
    """`async with connect("files", "search") as client:` gives a client for those groups.

    `connect("browser", settings={"browser": {"max_pages": 2}})` passes group settings. The
    groups are shut down when the block exits.
    """

    @asynccontextmanager
    async def _connect(*names: str, settings: dict[str, dict[str, Any]] | None = None) -> AsyncIterator[Client]:
        mcp, groups = build_server(rt, *names, settings=settings)
        try:
            async with Client(mcp) as client:
                yield client
        finally:
            for group in groups:
                await group.shutdown()

    return _connect


def text(result: types.CallToolResult) -> str:
    """All text blocks of a tool result joined, for assertions."""
    return "\n".join(b.text for b in result.content if isinstance(b, types.TextContent))
