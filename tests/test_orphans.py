"""The orphan guard: unconfined child groups die with a server that gets SIGKILL."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Starts a group, hands it to the guard and prints its pgid; the test then SIGKILLs this script.
_WATCH_THEN_WAIT = """\
import subprocess, sys, time
from dev_link_mcp import orphans
proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
orphans.watch(proc.pid)
if sys.argv[1] == "forget":
    orphans.forget(proc.pid)
print(proc.pid, flush=True)
time.sleep(300)
"""

# The same through the Sandbox under the `none` backend, with a background job in the group.
_SPAWN_THEN_WAIT = """\
import asyncio, sys, time
from pathlib import Path
from dev_link_mcp.config import Config
from dev_link_mcp.sandbox import Sandbox

async def main():
    ws, state = Path(sys.argv[1]), Path(sys.argv[2])
    config = Config(workspace=ws, state_dir=state, sandbox={"backend": "none"}).finalize()
    sb = Sandbox(config)
    with (state / "spawn.log").open("ab") as log_file:
        proc = await sb.spawn(sb.shell_argv("sleep 300 & sleep 300"), cwd=ws, log_file=log_file)
    print(proc.pid, flush=True)
    await asyncio.sleep(300)

asyncio.run(main())
"""


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_group_gone(pgid: int, timeout_s: float = 5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    return False


def _sigkill_after_pgid(argv: list[str]) -> int:
    """Run `argv`, read the pgid it prints, SIGKILL it and return the pgid."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        pgid = int(proc.stdout.readline())
        assert _group_alive(pgid)
    finally:
        proc.kill()
        proc.wait()
    return pgid


def test_a_watched_group_dies_with_its_server():
    pgid = _sigkill_after_pgid([sys.executable, "-c", _WATCH_THEN_WAIT, "watch"])
    try:
        assert _wait_group_gone(pgid)
    finally:
        if _group_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)


def test_a_forgotten_group_is_left_alone():
    pgid = _sigkill_after_pgid([sys.executable, "-c", _WATCH_THEN_WAIT, "forget"])
    try:
        # Long enough for the guard to have read EOF and acted.
        time.sleep(1)
        assert _group_alive(pgid)
    finally:
        if _group_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)


def test_an_unconfined_process_dies_with_a_killed_server(tmp_path: Path, workspace: Path):
    pgid = _sigkill_after_pgid([sys.executable, "-c", _SPAWN_THEN_WAIT, str(workspace), str(tmp_path / "state")])
    try:
        assert _wait_group_gone(pgid)
    finally:
        if _group_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)
