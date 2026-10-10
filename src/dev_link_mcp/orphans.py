"""Kill the server's unconfined child process groups if the server dies without cleaning up.

The normal shutdown stops every child itself. That cannot run after SIGKILL or a crash, and
only bubblewrap commands die with the server on their own (--die-with-parent). So the first
watch() starts a small guard process in its own session that holds the read end of a pipe.
The server tells it which process groups to kill and which ones it has since reaped. When the
server is gone, for whatever reason, the pipe closes and the guard kills the groups that are
still listed, then exits. A server that never calls watch() never starts the guard.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Runs with -I -S, so it imports nothing from the server and stays a few MB. Lines are
# "+ <pgid> [<scope cgroup dir>]" and "- <pgid>". Writing to cgroup.kill also takes down
# processes that left the group with setsid; the scope paths come from the limits code.
_GUARD = """\
import os, signal, sys
groups = {}
for line in sys.stdin:
    op, _, rest = line.rstrip("\\n").partition(" ")
    pgid, _, scope = rest.partition(" ")
    if op == "+":
        groups[int(pgid)] = scope
    elif op == "-":
        groups.pop(int(pgid), None)
for pgid, scope in groups.items():
    if scope:
        try:
            with open(os.path.join(scope, "cgroup.kill"), "w") as f:
                f.write("1")
        except OSError:
            pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
"""

_lock = threading.Lock()
_guard: subprocess.Popen[str] | None = None


def watch(pgid: int, scope: Path | None = None) -> None:
    """Have process group `pgid` (and the limits scope `scope`, if any) SIGKILLed if this process
    dies before it calls forget(pgid).

    Call it right after starting the group leader with start_new_session=True, and forget() once
    the leader was reaped and its group cleaned up. Starts the guard on first use. If the guard
    cannot run, logs a warning and the group is simply not covered.
    """
    _send(f"+ {pgid} {scope or ''}".rstrip() + "\n")


def forget(pgid: int) -> None:
    """Stop covering `pgid`: the server has cleaned it up itself. A no-op for an unknown group."""
    if _guard is not None:
        _send(f"- {pgid}\n")


def _send(line: str) -> None:
    global _guard
    with _lock:
        for _ in range(2):
            if _guard is None:
                try:
                    _guard = subprocess.Popen(
                        [sys.executable, "-I", "-S", "-c", _GUARD],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        # Its own session: Ctrl+C in a terminal and `dev-link stop` signal the
                        # server's group, and the guard has to outlive the server to do its job.
                        start_new_session=True,
                        text=True,
                    )
                except OSError as exc:
                    log.warning("cannot start the orphan guard; children may outlive a crashed server: %s", exc)
                    return
            assert _guard.stdin is not None
            try:
                _guard.stdin.write(line)
                _guard.stdin.flush()
                return
            except (BrokenPipeError, ValueError):
                # Something killed the guard. A fresh one gets this line, but not the groups the
                # old one knew about.
                log.warning("the orphan guard exited with code %s; starting a new one", _guard.poll())
                _guard = None
