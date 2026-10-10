"""Cleanup of the systemd limits scope when a command or background process ends.

Under the `none` backend a child that leaves the process group with setsid is only reachable
through the scope, so these tests use it.
"""

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dev_link_mcp.audit import AuditStore
from dev_link_mcp.config import Config
from dev_link_mcp.processes import ProcessManager
from dev_link_mcp.sandbox import Sandbox
from dev_link_mcp.sandbox.limits import ResourceLimits

needs_systemd = pytest.mark.skipif(
    ResourceLimits(64, 64).available() is not None, reason="systemd-run --user scopes are not usable here"
)
pytestmark = needs_systemd

DETACH = "</dev/null >/dev/null 2>&1"


def _sleepers(*args: str) -> list[int]:
    """Pids of live `sleep <arg>` processes."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().split(b"\0")
            state = (entry / "stat").read_text().split(")")[-1].split()[0]
        except OSError:
            continue
        if len(cmd) >= 2 and cmd[0].endswith(b"sleep") and cmd[1].decode() in args and state != "Z":
            found.append(int(entry.name))
    return found


async def _wait_sleepers(*args: str) -> list[int]:
    for _ in range(100):
        pids = _sleepers(*args)
        if len(pids) == len(args):
            return pids
        await asyncio.sleep(0.05)
    raise AssertionError(f"sleepers {args} did not start")


def _scope_of(pid: int) -> str:
    line = next(x for x in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if x.startswith("0::"))
    name = line.rsplit("/", 1)[-1]
    assert name.startswith("dev-link-cmd-"), line
    return name


def _unit_exists(unit: str) -> bool:
    out = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain", unit],
        capture_output=True,
        text=True,
    ).stdout
    return bool(out.strip())


@pytest.fixture
def limited_config(config: Config) -> Config:
    config.sandbox.backend = "none"
    config.limits.memory_mb = 2048
    return config


@pytest.fixture
async def pm(limited_config: Config) -> AsyncIterator[ProcessManager]:
    assert limited_config.state_dir is not None
    audit = AuditStore(limited_config.state_dir, secrets=[], result_bytes=1000)
    manager = ProcessManager(Sandbox(limited_config), audit, limited_config.state_dir, max_processes=8)
    try:
        yield manager
    finally:
        await manager.shutdown()
        audit.close()


async def test_run_empties_the_scope_when_the_command_returns(limited_config):
    sb = Sandbox(limited_config)
    r = await sb.run(sb.shell_argv(f"setsid sleep 3121 {DETACH} & echo done"), timeout_s=10)
    assert r.ok and r.stdout == "done\n", r.format()
    await asyncio.sleep(0.3)
    assert _sleepers("3121") == []


async def test_stop_empties_the_scope_after_sigterm(pm, workspace):
    managed = await pm.start(f"setsid sleep 3122 {DETACH} & sleep 3123", cwd=workspace)
    detached, _ = await _wait_sleepers("3122", "3123")
    unit = _scope_of(detached)
    await pm.stop(managed.id, grace_s=2)
    await asyncio.sleep(0.3)
    assert _sleepers("3122", "3123") == []
    assert not _unit_exists(unit)


async def test_process_that_exits_on_its_own_takes_its_scope_with_it(pm, workspace):
    managed = await pm.start(f"setsid sleep 3124 {DETACH} & sleep 0.5", cwd=workspace)
    (detached,) = await _wait_sleepers("3124")
    unit = _scope_of(detached)
    assert managed.watcher is not None
    await managed.watcher
    await asyncio.sleep(0.3)
    assert _sleepers("3124") == []
    assert not _unit_exists(unit)


async def test_shutdown_empties_the_scopes(pm, workspace):
    await pm.start(f"setsid sleep 3125 {DETACH} & sleep 3126", cwd=workspace)
    await _wait_sleepers("3125", "3126")
    await pm.shutdown()
    await asyncio.sleep(0.3)
    assert _sleepers("3125", "3126") == []
