"""Backend selection, the bwrap startup probe, and the optional systemd resource limits."""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import build_server, text
from mcp import Client

from dev_link_mcp.cli import main
from dev_link_mcp.config import Config
from dev_link_mcp.runtime import Runtime
from dev_link_mcp.sandbox import Sandbox, SandboxUnavailable, kill_tree
from dev_link_mcp.sandbox.backends import bubblewrap
from dev_link_mcp.sandbox.limits import ResourceLimits


def _no_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    real = shutil.which
    monkeypatch.setattr(bubblewrap.shutil, "which", lambda name, *a, **k: None if name == "bwrap" else real(name))


@pytest.mark.bwrap
def test_auto_picks_bubblewrap_on_linux(config):
    sb = Sandbox(config)
    assert sb.backend.name == "bubblewrap" and sb.enabled
    full, _ = sb._launch(["true"], config.workspace, None)
    assert Path(full[0]).name == "bwrap"


def test_auto_without_bwrap_refuses_with_both_fixes(config, monkeypatch):
    _no_bwrap(monkeypatch)
    # The fixture falls back to `none` where bwrap is missing; this test is about `auto` on Linux.
    config.sandbox.backend = "auto"
    monkeypatch.setattr(bubblewrap.sys, "platform", "linux")
    with pytest.raises(SandboxUnavailable) as exc:
        Sandbox(config)
    message = str(exc.value)
    assert "bubblewrap (bwrap) is not installed" in message
    assert "apt install bubblewrap" in message
    assert "--no-sandbox" in message and "sandbox.backend: none" in message


@pytest.mark.bwrap
def test_auto_with_broken_bwrap_refuses_with_apparmor_hint(config, monkeypatch):
    monkeypatch.setattr(bubblewrap, "_probe", lambda _: "bwrap: setting up uid map: Permission denied")
    with pytest.raises(SandboxUnavailable) as exc:
        Sandbox(config)
    message = str(exc.value)
    assert "setting up uid map: Permission denied" in message
    assert "kernel.apparmor_restrict_unprivileged_userns" in message
    assert "--no-sandbox" in message


@pytest.mark.parametrize("backend", ["auto", "bubblewrap"])
def test_bubblewrap_refuses_off_linux(config, monkeypatch, backend):
    config.sandbox.backend = backend
    monkeypatch.setattr(bubblewrap.sys, "platform", "darwin")
    with pytest.raises(SandboxUnavailable, match="bubblewrap only runs on Linux, not darwin"):
        Sandbox(config)


async def test_none_warns_and_works_without_bwrap(config, monkeypatch, caplog):
    _no_bwrap(monkeypatch)
    config.sandbox.backend = "none"
    with caplog.at_level(logging.WARNING):
        sb = Sandbox(config)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "full user access" in warnings[0].getMessage()
    assert sb.backend.name == "none" and not sb.enabled
    r = await sb.run(sb.shell_argv("echo ok"))
    assert r.ok and r.stdout == "ok\n"


def test_probe_reports_what_bwrap_printed(tmp_path):
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    fake.chmod(0o755)
    assert bubblewrap._probe(str(fake)) == "bwrap: No permissions to create new namespace"


@pytest.mark.bwrap
def test_probe_passes_with_the_real_bwrap():
    bwrap = shutil.which("bwrap")
    assert bwrap is not None
    assert bubblewrap._probe(bwrap) is None


def test_server_exits_with_the_refusal_message(workspace, tmp_path, monkeypatch, capsys):
    _no_bwrap(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["serve", str(workspace), "--state-dir", str(tmp_path / "state")])
    assert exc.value.code == 2
    assert (
        "dev-link: cannot start the bubblewrap sandbox: bubblewrap (bwrap) is not installed" in capsys.readouterr().err
    )


async def test_workspace_info_names_the_none_backend(tmp_path, workspace):
    cfg = Config(workspace=workspace, state_dir=tmp_path / "none-state", sandbox={"backend": "none"}).finalize()
    rt = Runtime.create(cfg)
    try:
        mcp, _ = build_server(rt, "system")
        async with Client(mcp) as client:
            out = text(await client.call_tool("workspace_info", {}))
    finally:
        await rt.processes.shutdown()
        rt.audit.close()
    assert "sandbox: none (OFF, full local access)" in out


# Resource limits


def _systemd_scopes_work() -> bool:
    return ResourceLimits(64, 64).available() is None


needs_systemd = pytest.mark.skipif(not _systemd_scopes_work(), reason="systemd-run --user scopes are not usable here")


def _limit_scopes() -> list[str]:
    out = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain", "dev-link-cmd-*"],
        capture_output=True,
        text=True,
    ).stdout
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def test_no_scope_without_limits(config):
    full, _ = Sandbox(config)._launch(["true"], config.workspace, None)
    assert "systemd-run" not in " ".join(full)


@pytest.mark.parametrize("backend", [pytest.param("auto", marks=pytest.mark.bwrap), "none"])
def test_scope_argv_when_a_limit_is_set(config, monkeypatch, backend):
    monkeypatch.setattr(ResourceLimits, "available", lambda self: None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/test")
    config.sandbox.backend = backend
    config.limits.max_pids = 50
    sb = Sandbox(config)
    full, proc_env = sb._launch(["true"], config.workspace, {"MY_VAR": "1"})
    assert Path(full[0]).name == "systemd-run"
    assert full[1:5] == ["--user", "--scope", "--quiet", "--collect"]
    assert full[5].startswith("--unit=dev-link-cmd-")
    assert full[6:9] == ["-p", "TasksMax=50", "--"]
    assert "MemoryMax" not in " ".join(full)
    assert proc_env["XDG_RUNTIME_DIR"] == "/run/user/test"
    if backend == "auto":
        # The sandboxed environment has no XDG_RUNTIME_DIR; it is removed again before bwrap starts.
        assert full[9:13] == ["/usr/bin/env", "-u", "XDG_RUNTIME_DIR", sb.backend.bwrap]  # type: ignore[attr-defined]
        assert "MY_VAR" not in proc_env
    else:
        assert full[9:] == ["true"]
        assert proc_env["MY_VAR"] == "1"


def test_memory_limit_disables_swap(config, monkeypatch):
    monkeypatch.setattr(ResourceLimits, "available", lambda self: None)
    config.limits.memory_mb = 256
    full, _ = Sandbox(config)._launch(["true"], config.workspace, None)
    assert full[6:10] == ["-p", "MemoryMax=256M", "-p", "MemorySwapMax=0"]


def test_limit_refuses_to_start_without_a_user_manager(config, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(config.state_dir / "no-such-runtime-dir"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    config.limits.memory_mb = 256
    with pytest.raises(SandboxUnavailable, match=r"cannot enforce limits\.memory_mb / limits\.max_pids: .*unset both"):
        Sandbox(config)


def test_limit_refuses_without_systemd_run(config, monkeypatch):
    real = shutil.which
    monkeypatch.setattr(
        "dev_link_mcp.sandbox.limits.shutil.which",
        lambda name, *a, **k: None if name == "systemd-run" else real(name, *a, **k),
    )
    config.limits.max_pids = 64
    with pytest.raises(SandboxUnavailable, match="systemd-run is not installed"):
        Sandbox(config)


@needs_systemd
async def test_memory_limit_kills_a_command_over_it(config):
    config.limits.memory_mb = 64
    sb = Sandbox(config)
    r = await sb.run(["python3", "-c", "b = bytearray(400 * 1024 * 1024); print('allocated')"], timeout_s=30)
    assert not r.ok and "allocated" not in r.stdout, r.format()
    assert r.exit_code in (137, -9), r.format()
    small = await sb.run(["python3", "-c", "b = bytearray(16 * 1024 * 1024); print('allocated')"], timeout_s=30)
    assert small.ok and small.stdout == "allocated\n", small.format()
    await asyncio.sleep(0.3)
    assert _limit_scopes() == []


@needs_systemd
async def test_pid_limit_stops_a_fork_loop(config):
    config.limits.max_pids = 20
    sb = Sandbox(config)
    script = (
        "import os, time\n"
        "n = 0\n"
        "try:\n"
        "    for _ in range(100):\n"
        "        if os.fork() == 0:\n"
        "            time.sleep(2); os._exit(0)\n"
        "        n += 1\n"
        "except OSError:\n"
        "    print('fork failed after', n)\n"
    )
    r = await sb.run(["python3", "-c", script], timeout_s=30)
    assert "fork failed after" in r.stdout, r.format()
    assert int(r.stdout.split()[-1]) < 20


def _sleepers(*args: str) -> list[int]:
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


@needs_systemd
@pytest.mark.parametrize(("backend", "base"), [pytest.param("auto", 3060, marks=pytest.mark.bwrap), ("none", 3070)])
async def test_timeout_empties_the_scope(config, backend, base):
    # Under `none` only the scope catches a child that left the process group with setsid.
    config.sandbox.backend = backend
    config.limits.max_pids = 64
    sb = Sandbox(config)
    detached, waiting = str(base + 1), str(base)
    r = await sb.run(
        sb.shell_argv(f"setsid sleep {detached} </dev/null >/dev/null 2>&1 & sleep {waiting}"), timeout_s=1
    )
    assert r.timed_out
    await asyncio.sleep(0.3)
    assert _sleepers(detached, waiting) == []
    assert _limit_scopes() == []


@needs_systemd
async def test_kill_tree_empties_the_scope_of_a_spawned_process(config, tmp_path):
    config.sandbox.backend = "none"
    config.limits.memory_mb = 256
    sb = Sandbox(config)
    with (tmp_path / "spawn.log").open("ab") as log_file:
        proc = await sb.spawn(
            sb.shell_argv("setsid sleep 3081 </dev/null >/dev/null 2>&1 & sleep 3080"),
            cwd=config.workspace,
            log_file=log_file,
        )
    try:
        for _ in range(50):
            if len(_sleepers("3080", "3081")) == 2:
                break
            await asyncio.sleep(0.05)
        assert len(_sleepers("3080", "3081")) == 2
    finally:
        # Also on a failed assertion, so a broken run does not leave its scope behind.
        kill_tree(proc.pid)
        await proc.wait()
    await asyncio.sleep(0.3)
    assert _sleepers("3080", "3081") == []
    assert _limit_scopes() == []


# Spawns a command with a memory limit whose background job leaves the group with setsid, then
# waits to be SIGKILLed.
_SPAWN_IN_SCOPE_THEN_WAIT = """\
import asyncio, sys
from pathlib import Path
from dev_link_mcp.config import Config
from dev_link_mcp.sandbox import Sandbox

async def main():
    ws, state = Path(sys.argv[1]), Path(sys.argv[2])
    config = Config(workspace=ws, state_dir=state, sandbox={"backend": "none"}, limits={"memory_mb": 256}).finalize()
    sb = Sandbox(config)
    with (state / "spawn.log").open("ab") as log_file:
        await sb.spawn(
            sb.shell_argv("setsid sleep 3091 </dev/null >/dev/null 2>&1 & sleep 3090"), cwd=ws, log_file=log_file
        )
    print("started", flush=True)
    await asyncio.sleep(300)

asyncio.run(main())
"""


@needs_systemd
def test_a_killed_server_empties_the_scope_of_an_unconfined_process(tmp_path, workspace):
    server = subprocess.Popen(
        [sys.executable, "-c", _SPAWN_IN_SCOPE_THEN_WAIT, str(workspace), str(tmp_path / "state")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert server.stdout is not None
        assert server.stdout.readline().strip() == "started"
        for _ in range(50):
            if len(_sleepers("3090", "3091")) == 2:
                break
            time.sleep(0.05)
        assert len(_sleepers("3090", "3091")) == 2
    finally:
        server.kill()
        server.wait()
    for _ in range(50):
        if not _sleepers("3090", "3091"):
            break
        time.sleep(0.1)
    leftover = _sleepers("3090", "3091")
    for pid in leftover:
        os.kill(pid, signal.SIGKILL)
    assert leftover == []
