"""Memory and process limits for agent commands, enforced by a transient systemd user scope.

`systemd-run --scope` execs the command in place, so the pid and process group the Sandbox
started stay the same and only the cgroup changes. The scope's cgroup holds every process the
command starts, including bwrap and anything that detaches with setsid.
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
# Reads back the limits the kernel applied, from inside the probe scope.
_READ_LIMITS = (
    'cg=$(sed -n "s/^0:://p" /proc/self/cgroup); cat "/sys/fs/cgroup$cg/memory.max" "/sys/fs/cgroup$cg/pids.max"'
)


class ResourceLimits:
    """`limits.memory_mb` and `limits.max_pids`; inactive when both are None."""

    def __init__(self, memory_mb: int | None, max_pids: int | None):
        self.memory_mb = memory_mb
        self.max_pids = max_pids
        self.systemd_run = shutil.which("systemd-run")

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
        applied = done.stdout.split()
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
        return None

    def wrap(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        """(argv, env) that run `argv` in a new scope with the limits.

        systemd-run finds the user manager through XDG_RUNTIME_DIR. When `env` lacks it (the
        sandboxed environment does), it is added for systemd-run and removed again before the
        command starts, so the command sees exactly `env`.
        """
        assert self.systemd_run is not None
        unit = f"{_UNIT_PREFIX}{secrets.token_hex(6)}"
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
    commands under the `none` backend that left the process group with setsid.
    """
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return
    for line in cgroup.splitlines():
        if not line.startswith("0::"):
            continue
        path = _CGROUP_ROOT / line[3:].lstrip("/")
        if _SCOPE_NAME.fullmatch(path.name):
            try:
                (path / "cgroup.kill").write_text("1")
            except OSError as exc:
                # Kernels before 5.14 have no cgroup.kill; the process group was signalled anyway.
                log.debug("cannot kill scope %s: %s", path.name, exc)
