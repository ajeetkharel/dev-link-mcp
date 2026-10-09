"""`dev-link tools` and `dev-link install-browser`."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from dev_link_mcp.app import assemble
from dev_link_mcp.cli.common import CliError, resolve
from dev_link_mcp.sandbox import SandboxUnavailable
from dev_link_mcp.tools import ToolGroupError


def _summary(description: str, limit: int = 100) -> str:
    """The first sentence of a tool description on one line, cut to `limit` characters."""
    text = " ".join(description.split())
    sentence = text.split(". ", 1)[0].rstrip(".") + "."
    return sentence if len(sentence) <= limit else sentence[: limit - 3].rstrip() + "..."


async def _listing(args: argparse.Namespace) -> dict[str, Any]:
    # A throwaway state dir, no sandbox and no resource limits: the listing must not touch a
    # running server's audit store or private /tmp, and needs no bwrap or systemd-run probe.
    # Tool registration depends on none of them.
    with tempfile.TemporaryDirectory(prefix="dev-link-tools-") as scratch:
        args.state_dir = Path(scratch)
        args.no_sandbox = True
        config = resolve(args)
        config.limits.memory_mb = None
        config.limits.max_pids = None
        mcp, rt, toolset = await assemble(config)
        try:
            described = {t.name: _summary(t.description or "") for t in await mcp.list_tools()}
        finally:
            await toolset.shutdown()
            await rt.processes.shutdown()
            rt.audit.close()
    groups = {
        group: [{"name": name, "description": described.get(name, "")} for name in names]
        for group, names in toolset.tools.items()
    }
    return {"groups": groups, "unavailable": toolset.unavailable, "disabled": toolset.disabled}


def cmd_tools(args: argparse.Namespace) -> int:
    # The sandbox backend `none` logs a warning meant for a real server.
    logger = logging.getLogger("dev_link_mcp")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        listing = asyncio.run(_listing(args))
    except (ToolGroupError, SandboxUnavailable) as exc:
        raise CliError.from_exc(exc) from None
    finally:
        logger.setLevel(level)
    if args.group:
        known = [*listing["groups"], *listing["unavailable"], *listing["disabled"]]
        if args.group not in known:
            raise CliError(f"unknown tool group {args.group!r}; known groups: {', '.join(sorted(known))}")
        listing = {
            "groups": {k: v for k, v in listing["groups"].items() if k == args.group},
            "unavailable": {k: v for k, v in listing["unavailable"].items() if k == args.group},
            "disabled": [g for g in listing["disabled"] if g == args.group],
        }
    if args.json:
        print(json.dumps(listing, indent=2))
        return 0
    lines = []
    for group, tools in listing["groups"].items():
        lines.append(f"{group} ({len(tools)})")
        width = max((len(t["name"]) for t in tools), default=0)
        lines += [f"  {t['name']:<{width}}  {t['description']}" for t in tools]
    for group, hint in listing["unavailable"].items():
        lines.append(f"{group}: not installed; {hint}")
    for group in listing["disabled"]:
        lines.append(f"{group}: off")
    print("\n".join(lines))
    return 0


def cmd_install_browser(args: argparse.Namespace) -> int:
    if importlib.util.find_spec("playwright") is None:
        raise CliError("Playwright is not installed; install it with: pip install 'dev-link-mcp[browser]'")
    return subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False).returncode
