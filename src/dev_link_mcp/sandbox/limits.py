"""Memory and process limits for agent commands, enforced by a transient systemd user scope.

`systemd-run --scope` execs the command in place, so the pid and process group the Sandbox
started stay the same and only the cgroup changes. The scope's cgroup holds every process the
command starts, including bwrap and anything that detaches with setsid, so killing the scope
when the command ends takes all of them down.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_UNIT_PREFIX = "dev-link-cmd-"
_SCOPE_NAME = re.compile(re.escape(_UNIT_PREFIX) + r"[0-9a-f]+\.scope")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
# Reads back the limits the kernel applied, then the probe scope's own cgroup path.
_READ_LIMITS = (
    'cg=$(sed -n "s/^0:://p" /proc/self/cgroup); cat "/sys/fs/cgroup$cg/memory.max" "/sys/fs/cgroup$cg/pids.max";'
    ' echo "$cg"'
)


class ResourceLimits:
    """`limits.memory_mb` and `limits.max_pids`; inactive when both are None."""

    def __init__(self, memory_mb: int | None, max_pids: int | None):
        self.memory_mb = memory_mb
        self.max_pids = max_pids
        self.systemd_run = shutil.which("systemd-run")
        # The cgroup directory that holds our transient scopes, learned by available(); None
        # until then. Lets a scope be found by its unit name after its first process is gone.
        self.scope_parent: Path | None = None

    @property
    def active(self) -> bool:
        return self.memory_mb is not None or self.max_pids is not None

    def _properties(self) -> list[str]:
        props = []
        if self.memory_mb is not None:
            # Without a swap cap the kernel swaps the command out instead of stopping it.
            props += ["-p", f"MemoryMax={self.memory_mb}M", "-p", "MemorySwapMax=0"]
            # The default OOMPolicy=stop sends SIGTERM to the rest of the scope after an OOM kill,
            # racing the sandbox wrapper, so the command could report -15 instead of the OOM kill.
            props += ["-p", "OOMPolicy=continue"]
        if self.max_pids is not None:
            props += ["-p", f"TasksMax={self.max_pids}"]
        return props

    def available(self) -> str | None:
        """None when a test scope gets the configured limits, else a one-line reason.

        Starts one short-lived scope (about 15 ms). A missing user manager, a missing D-Bus
        session or a cgroup controller that is not delegated to the user all show up here.
        """
        if not sys.platform.startswith("linux"):
            return f"they need systemd on Linux, not {sys.platform}"
        if self.systemd_run is None:
            return "systemd-run is not installed"
        argv = [self.systemd_run, "--user", "--scope", "--quiet", "--collect", *self._properties()]
        try:
            done = subprocess.run(
                [*argv, "--", "/bin/sh", "-c", _READ_LIMITS], capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired:
            return "systemd-run --user did not finish a test run within 10 s"
        except OSError as exc:
            return str(exc)
        if done.returncode != 0:
            lines = done.stderr.strip().splitlines()
            return f"systemd-run --user failed ({lines[0] if lines else f'exit code {done.returncode}'})"
        lines = done.stdout.split()
        applied, own = lines[:-1], lines[-1:]
        # An unset limit reads back as "max" or systemd's DefaultTasksMax, so only set ones are compared.
        expected = [
            str(self.memory_mb * 1024 * 1024) if self.memory_mb is not None else None,
            str(self.max_pids) if self.max_pids is not None else None,
        ]
        if len(applied) != 2 or any(
            want is not None and got != want for got, want in zip(applied, expected, strict=True)
        ):
            return (
                f"the systemd user manager did not apply them (cgroup reports memory.max and pids.max as"
                f" {' '.join(applied) or 'unreadable'}); the memory and pids controllers must be delegated to it"
            )
        # Every `systemd-run --user --scope` without --slice lands in the same parent cgroup.
        if own and own[0].startswith("/") and ".." not in own[0].split("/"):
            self.scope_parent = (_CGROUP_ROOT / own[0].lstrip("/")).parent
        return None

    @staticmethod
    def new_unit() -> str:
        """A fresh, unique scope unit name for one command."""
        return f"{_UNIT_PREFIX}{secrets.token_hex(6)}"

    def scope_dir(self, unit: str) -> Path | None:
        """cgroup directory of the scope `unit`, or None when available() has not located the scopes."""
        if self.scope_parent is None:
            return None
        return self.scope_parent / f"{unit}.scope"

    def wrap(self, argv: list[str], env: dict[str, str], unit: str | None = None) -> tuple[list[str], dict[str, str]]:
        """(argv, env) that run `argv` in a new scope with the limits.

        `unit` names the scope (see new_unit); a fresh name is used when it is None.

        systemd-run finds the user manager through XDG_RUNTIME_DIR. When `env` lacks it (the
        sandboxed environment does), it is added for systemd-run and removed again before the
        command starts, so the command sees exactly `env`.
        """
        assert self.systemd_run is not None
        unit = unit or self.new_unit()
        # --collect removes the unit even when it failed, e.g. after an OOM kill; otherwise
        # every killed command would leave a failed scope behind.
        prefix = [self.systemd_run, "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
        prefix += [*self._properties(), "--"]
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if "XDG_RUNTIME_DIR" not in env and runtime_dir:
            env = {**env, "XDG_RUNTIME_DIR": runtime_dir}
            prefix += ["/usr/bin/env", "-u", "XDG_RUNTIME_DIR"]
        return prefix + argv, env


def kill_scope(pid: int) -> None:
    """SIGKILL every process in the limits scope of `pid`, if it runs in one.

    The bubblewrap PID namespace already takes its processes down with it; this catches
    commands under the `none` backend that left the process group with setsid. Needs `pid`
    alive to find the scope; kill_scope_dir works after it is gone.
    """
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return
    for line in cgroup.splitlines():
        if line.startswith("0::"):
            kill_scope_dir(_CGROUP_ROOT / line[3:].lstrip("/"))


def kill_scope_dir(path: Path) -> None:
    """SIGKILL every process in the scope cgroup `path`; a no-op when it is gone or not one of ours."""
    if not _SCOPE_NAME.fullmatch(path.name):
        return
    try:
        (path / "cgroup.kill").write_text("1")
    except FileNotFoundError:
        # Usually --collect already removed the emptied scope; a scope without cgroup.kill
        # means a kernel before 5.14, where only the process group gets signalled.
        if path.is_dir():
            log.debug("cannot kill scope %s: no cgroup.kill", path.name)
    except OSError as exc:
        # The process group was signalled anyway.
        log.debug("cannot kill scope %s: %s", path.name, exc)
