"""Bubblewrap backend: each command runs in fresh Linux namespaces confined to the workspace.

Inside the sandbox the workspace is the only writable host directory. System directories and
configured toolchains are read-only, the real home directory is replaced by a private one
under the state dir, and paths match host paths so tool output stays meaningful to the agent.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from dev_link_mcp.config import Config

log = logging.getLogger(__name__)

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
    namespaces it creates, and a container can forbid mounting /proc, which every command
    needs. Cached because the answer does not change while the server runs.
    """
    try:
        done = subprocess.run(
            [
                bwrap,
                "--unshare-all",
                "--die-with-parent",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "true",
            ],
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
        # Paths already reported as skipped, so a command loop does not repeat the warning.
        self._skipped: set[Path] = set()

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
        """Configured read-only paths that exist and keep clear of the state dir, resolved."""
        return self._mountable(self.config.sandbox.ro_paths, "sandbox.ro_paths entry")

    def rw_paths(self) -> list[Path]:
        """Configured read-write paths that exist and keep clear of the state dir, resolved."""
        return self._mountable(self.config.sandbox.rw_paths, "sandbox.rw_paths entry")

    def _mountable(self, raws: Sequence[str], source: str) -> list[Path]:
        """The entries of `raws` that exist, resolved, minus those that would mount the state dir.

        Resolved again for every command: an entry that did not exist at startup can since have
        been created inside a writable mount as a symlink to the state dir, which exposed_state()
        could not see. Such an entry is left out and logged once.
        """
        paths = []
        for raw in raws:
            p = Path(raw).expanduser()
            if not p.exists():
                continue
            root = p.resolve()
            if self._state_overlap(root) is None:
                paths.append(root)
            elif root not in self._skipped:
                self._skipped.add(root)
                log.warning("not mounting %s %s: it resolves to %s, which overlaps state_dir", source, raw, root)
        return paths

    def _state_overlap(self, root: Path) -> str | None:
        """How mounting the resolved path `root` would expose the state dir, or None when it would not.

        "contains" when the state dir is inside `root`, "inside" when `root` is inside the state
        dir. Mounts inside its `home` and `tmp` do not count: those are the agent's own.
        """
        if self.config.state_dir is None:
            return None
        state = self.config.state_dir.resolve()
        if state.is_relative_to(root):
            return "contains"
        own = (self.home.resolve(), self.tmp.resolve())
        if root.is_relative_to(state) and not any(root.is_relative_to(p) for p in own):
            return "inside"
        return None

    def exposed_state(self) -> str | None:
        """None when commands cannot see the state dir, else a one-line reason naming the mount.

        The state dir holds the audit log, process logs and the connection file with the token,
        so a mount that contains it or lies inside it would let the agent read them (or rewrite
        them, for a read-write mount). Its `home` and `tmp` are the agent's own and are meant to
        be mounted; a mount inside them exposes nothing else. Configured paths count whether or
        not they exist yet, because `wrap` mounts them as soon as they do.
        """
        state = self.config.state_dir
        if state is None:
            return None
        state = state.resolve()
        mounts = [(Path(p), "built-in system mount") for p in (*_SYSTEM_RO, *_SYSTEM_LINKS, "/etc/resolv.conf")]
        mounts += [(Path(raw).expanduser(), "sandbox.ro_paths entry") for raw in self.config.sandbox.ro_paths]
        mounts += [(Path(raw).expanduser(), "sandbox.rw_paths entry") for raw in self.config.sandbox.rw_paths]
        mounts.append((self.config.workspace, "workspace"))
        for path, source in mounts:
            root = path.resolve()
            overlap = self._state_overlap(root)
            if overlap is None:
                continue
            if overlap == "contains":
                where = f"state_dir {state} is inside {source} {root}"
            else:
                where = f"{source} {root} is inside state_dir {state}"
            return (
                f"{where}, so sandboxed commands could read the audit log and the connection token; "
                "set state_dir (--state-dir) to a folder the sandbox does not mount, or change that setting"
            )
        return None

    def replaceable_mount(self) -> str | None:
        """None when the agent cannot change any configured mount source, else a one-line reason.

        The agent writes the workspace, every rw_paths entry and its private home and tmp. A
        ro_paths or rw_paths entry inside (or equal to) one of them could be swapped for a
        symlink between the check in wrap() and the mount bwrap then makes, mounting whatever
        it points to. Paths are compared after expanduser and resolve, existing or not.
        """
        ro = [(Path(raw).expanduser(), "sandbox.ro_paths entry") for raw in self.config.sandbox.ro_paths]
        rw = [(Path(raw).expanduser(), "sandbox.rw_paths entry") for raw in self.config.sandbox.rw_paths]
        own = [(self.config.workspace, "workspace"), (self.home, "private home"), (self.tmp, "private tmp")]
        for entry in ro + rw:
            path, source = entry
            root = path.resolve()
            for mount in own + rw:
                if mount is entry or not root.is_relative_to(mount[0].resolve()):
                    continue
                return (
                    f"{source} {path} is inside the writable mount {mount[1]} {mount[0].resolve()}, so a sandboxed "
                    "command could replace it with a symlink to any host path; remove that entry or move it out of "
                    "the writable mount"
                )
        return None

    def visible(self, path: Path) -> bool:
        roots = [Path(p) for p in _SYSTEM_RO] + self.ro_paths() + self.rw_paths() + [self.config.workspace]
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
        for p in self.rw_paths():
            args += ["--bind", str(p), str(p)]
        args += ["--bind", str(self.config.workspace), str(self.config.workspace)]
        for name, value in extra_env.items():
            args += ["--setenv", name, value]
        args += ["--chdir", str(cwd), "--"]
        return args + list(argv)
