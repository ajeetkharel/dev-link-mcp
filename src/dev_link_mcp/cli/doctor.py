"""`dev-link doctor`: check that a server with these settings can start and every tool group works.

It never starts the server or opens the audit store. Like every command that resolves the
config it creates the state dir if it is missing, and it starts Playwright's driver for a
moment to find Chromium.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Literal, NamedTuple

from dev_link_mcp.cli import instance
from dev_link_mcp.cli.common import config_from_args
from dev_link_mcp.config import Config
from dev_link_mcp.sandbox.backends.bubblewrap import Bubblewrap
from dev_link_mcp.sandbox.limits import ResourceLimits
from dev_link_mcp.tools import BUILTINS, install_hint, missing_requirements

Status = Literal["ok", "warn", "fail"]


class Check(NamedTuple):
    status: Status
    name: str
    detail: str
    fix: str = ""


def _chromium() -> str | None:
    """Path of the Chromium Playwright would launch, or None when it is not installed. Starts
    Playwright's driver for a moment, since only it knows where its browsers live."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        path = p.chromium.executable_path
    return path if path and Path(path).exists() else None


def _programs(tunnel: bool) -> list[Check]:
    checks = []
    for name, fix, needed in (
        ("git", "install git; checkpoints and apply_patch need it", False),
        ("rg", "install ripgrep (e.g. apt install ripgrep or brew install ripgrep); the search tools need it", False),
        ("cloudflared", "install cloudflared (e.g. brew install cloudflared); only --tunnel needs it", tunnel),
    ):
        path = shutil.which(name)
        if path:
            checks.append(Check("ok", name, path))
        else:
            checks.append(Check("fail" if needed else "warn", name, "not found on PATH", fix))
    return checks


def _sandbox(config: Config) -> Check:
    if not config.sandbox.enabled:
        source = config.sources.get("sandbox.backend", "config")
        return Check(
            "warn",
            "sandbox",
            f"off (sandbox.backend none, set by {source}): agent commands run with your full user access",
            "drop --no-sandbox (or sandbox.backend: none) to confine commands to the workspace",
        )
    assert config.state_dir is not None
    backend = Bubblewrap(config, home=config.state_dir / "home", tmp=config.state_dir / "tmp")
    exposed = backend.exposed_state()
    if exposed is not None:
        return Check("fail", "sandbox", exposed, "move state_dir out of every folder the sandbox mounts")
    replaceable = backend.replaceable_mount()
    if replaceable is not None:
        return Check("fail", "sandbox", replaceable, "move that path out of the workspace, rw_paths and state_dir")
    reason = backend.available()
    if reason is None:
        return Check("ok", "sandbox", f"bubblewrap ({backend.bwrap}) can create a sandbox")
    if backend.bwrap is not None and sys.platform.startswith("linux"):
        fix = (
            "allow unprivileged user namespaces for bwrap (on Ubuntu see kernel.apparmor_restrict_unprivileged_userns"
            " or an AppArmor profile for bwrap), or run with --no-sandbox"
        )
    elif sys.platform.startswith("linux"):
        fix = "install bubblewrap (e.g. apt install bubblewrap), or run with --no-sandbox"
    else:
        fix = "run with --no-sandbox; the bubblewrap sandbox needs Linux"
    return Check("fail", "sandbox", reason, fix)


def _limits(config: Config) -> Check:
    limits = ResourceLimits(config.limits.memory_mb, config.limits.max_pids)
    reason = limits.available()
    if reason is None:
        what = "enforced" if limits.active else "available (no limits set)"
        return Check("ok", "limits", f"systemd user scopes work; limits.memory_mb / limits.max_pids {what}")
    if limits.active:
        return Check("fail", "limits", reason, "unset limits.memory_mb and limits.max_pids, or fix systemd --user")
    return Check("warn", "limits", reason, "only needed for limits.memory_mb / limits.max_pids")


def _groups(config: Config) -> list[Check]:
    disabled = set(config.tools.disabled)
    checks = []
    for b in BUILTINS:
        if not b.requires or b.name in disabled:
            continue
        missing = missing_requirements(b)
        if missing:
            checks.append(Check("warn", b.name, f"{', '.join(missing)} not installed", install_hint(b)))
        elif b.name == "browser":
            checks.append(_browser())
        else:
            checks.append(Check("ok", b.name, f"{', '.join(b.requires)} installed"))
    if "docker" in config.tools.enabled and "docker" not in disabled:
        path = shutil.which("docker")
        if path:
            checks.append(Check("ok", "docker", path))
        else:
            checks.append(
                Check("fail", "docker", "docker group is enabled but docker is not on PATH", "install the docker CLI")
            )
    return checks


def _browser() -> Check:
    # Playwright installs without its browser; only its driver knows whether Chromium is there.
    try:
        path = _chromium()
    except Exception as exc:
        lines = str(exc).strip().splitlines() or [type(exc).__name__]
        return Check("warn", "browser", f"Playwright could not start: {lines[0]}", "dev-link install-browser")
    if path:
        return Check("ok", "browser", f"Chromium at {path}")
    return Check("warn", "browser", "Chromium is not installed", "dev-link install-browser")


def _state_and_port(config: Config) -> list[Check]:
    state = config.state_dir
    assert state is not None
    checks = []
    if os.access(state, os.W_OK | os.X_OK):
        checks.append(Check("ok", "state dir", str(state)))
    else:
        checks.append(Check("fail", "state dir", f"{state} is not writable", "fix its permissions or pass --state-dir"))
    host, port = config.server.host, config.server.port
    pid = instance.holder(state)
    if pid is not None:
        checks.append(Check("ok", "port", f"this workspace's server is running (PID {pid})"))
    elif instance.port_free(host, port):
        checks.append(Check("ok", "port", f"{host}:{port} is free"))
    elif config.sources.get("server.port", "default") == "default":
        checks.append(
            Check("warn", "port", f"{host}:{port} is in use", "dev-link start picks the next free port by itself")
        )
    else:
        source = config.sources["server.port"]
        checks.append(Check("fail", "port", f"{host}:{port} is in use (set by {source})", "choose another --port"))
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = [Check("ok", "python", f"{sys.version.split()[0]} ({sys.executable})")]
    config: Config | None = None
    try:
        config = config_from_args(args)
    except (ValueError, OSError) as exc:
        lines = str(exc).splitlines() or [type(exc).__name__]
        checks.append(Check("fail", "config", "; ".join(lines), "fix the setting named above, at the source shown"))
    if config is not None:
        checks.append(Check("ok", "config", f"workspace {config.workspace}"))
        checks.append(_sandbox(config))
        checks.append(_limits(config))
    checks += _programs(bool(args.tunnel))
    if config is not None:
        checks += _groups(config)
        checks += _state_and_port(config)
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"{check.status:<4}  {check.name:<{width}}  {check.detail}")
        if check.status != "ok" and check.fix:
            print(f"{'':<4}  {'':<{width}}  fix: {check.fix}")
    return 1 if any(c.status == "fail" for c in checks) else 0
