"""Bubblewrap backend: each command runs in fresh Linux namespaces confined to the workspace.

Inside the sandbox the workspace is the only writable host directory. System directories and
configured toolchains are read-only, the real home directory is replaced by a private one
under the state dir, and paths match host paths so tool output stays meaningful to the agent.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from dev_link_mcp.config import Config

_SYSTEM_RO = ("/usr", "/etc", "/opt", "/srv")
_SYSTEM_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")

_APPARMOR_HINT = (
    "on Ubuntu 23.10 and later AppArmor blocks the unprivileged user namespaces bwrap needs; see your "
    "distribution's docs on kernel.apparmor_restrict_unprivileged_userns or an AppArmor profile for bwrap"
)


@functools.cache
def _probe(bwrap: str) -> str | None:
    """Run one throwaway sandbox. None when it works, else the first line bwrap printed.

    `bwrap` existing is not enough: AppArmor, a container or a sysctl can forbid the
    namespaces it creates. Cached because the answer does not change while the server runs.
    """
    try:
        done = subprocess.run(
            [bwrap, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return "bwrap did not finish a test run within 5 s"
    except OSError as exc:
        return str(exc)
    if done.returncode == 0:
        return None
    lines = done.stderr.strip().splitlines()
    return lines[0] if lines else f"bwrap exited with code {done.returncode}"


class Bubblewrap:
    name = "bubblewrap"
    confined = True

    def __init__(self, config: Config, *, home: Path, tmp: Path):
        self.config = config
        self.home = home
        self.tmp = tmp
        self.real_home = Path.home()
        self.bwrap = shutil.which("bwrap")

    def available(self) -> str | None:
        if not sys.platform.startswith("linux"):
            return f"bubblewrap only runs on Linux, not {sys.platform}"
        if self.bwrap is None:
            return "bubblewrap (bwrap) is not installed; install it (e.g. apt install bubblewrap)"
        failure = _probe(self.bwrap)
        if failure is not None:
            return f"bwrap is installed but cannot create a sandbox ({failure}); {_APPARMOR_HINT}"
        return None

    def ro_paths(self) -> list[Path]:
        """Configured read-only paths that exist, resolved."""
        paths = []
        for raw in self.config.sandbox.ro_paths:
            p = Path(raw).expanduser()
            if p.exists():
                paths.append(p.resolve())
        return paths

    def visible(self, path: Path) -> bool:
        roots = [Path(p) for p in _SYSTEM_RO] + self.ro_paths() + [self.config.workspace]
        roots += [Path(p).expanduser().resolve() for p in self.config.sandbox.rw_paths]
        resolved = path.resolve()
        return any(resolved.is_relative_to(root) for root in roots)

    def wrap(self, argv: Sequence[str], cwd: Path, extra_env: Mapping[str, str]) -> list[str]:
        """Prefix argv with the bwrap invocation.

        `extra_env` is applied with --setenv, so it only exists inside the sandbox. It must
        never be in the environment of bwrap itself: bwrap is not setuid, so LD_PRELOAD or
        LD_LIBRARY_PATH there would load agent-built code on the host before confinement.
        """
        assert self.bwrap is not None
        args = [self.bwrap, "--die-with-parent", "--new-session", "--unshare-all", "--hostname", "dev-link"]
        if self.config.sandbox.network:
            args.append("--share-net")
        for path in _SYSTEM_RO:
            if os.path.exists(path):
                args += ["--ro-bind", path, path]
        for path in _SYSTEM_LINKS:
            if os.path.islink(path):
                args += ["--symlink", os.readlink(path), path]
            elif os.path.isdir(path):
                args += ["--ro-bind", path, path]
        resolv = Path("/etc/resolv.conf")
        if resolv.is_symlink():
            # systemd-resolved points this into /run, which is otherwise not mounted.
            target = resolv.resolve()
            if target.exists():
                args += ["--ro-bind", str(target), str(target)]
        # /tmp is shared by all commands of one server run so multi-step work can stage
        # files there; it is emptied at server start.
        args += ["--proc", "/proc", "--dev", "/dev", "--bind", str(self.tmp), "/tmp", "--tmpfs", "/run"]
        # Private home first; toolchains and the workspace are mounted on top of it.
        args += ["--bind", str(self.home), str(self.real_home)]
        for p in self.ro_paths():
            args += ["--ro-bind", str(p), str(p)]
        for raw in self.config.sandbox.rw_paths:
            p = Path(raw).expanduser().resolve()
            if p.exists():
                args += ["--bind", str(p), str(p)]
        args += ["--bind", str(self.config.workspace), str(self.config.workspace)]
        for name, value in extra_env.items():
            args += ["--setenv", name, value]
        args += ["--chdir", str(cwd), "--"]
        return args + list(argv)
