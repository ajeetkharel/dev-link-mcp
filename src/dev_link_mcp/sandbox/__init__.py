"""Run agent commands through the configured sandbox backend.

The facade owns what every backend shares: the environment (rebuilt from an allowlist
when confined), the private home and /tmp, PATH, timeouts, output capture, optional
resource limits and process cleanup. The backend only says how a command is confined.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from mcp.server.mcpserver.exceptions import ToolError

from dev_link_mcp.config import Config
from dev_link_mcp.sandbox.backends import Backend, SandboxUnavailable, select
from dev_link_mcp.sandbox.limits import ResourceLimits, kill_scope, kill_scope_dir

__all__ = ["Capture", "RunResult", "Sandbox", "SandboxUnavailable", "kill_group_rest", "kill_tree", "pump"]

log = logging.getLogger(__name__)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STD_PATH = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    truncated: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def format(self) -> str:
        """Agent-facing text: status line, then stdout and stderr sections when non-empty."""
        if self.timed_out:
            status = f"TIMED OUT after {self.duration_s:.1f}s (process killed)"
        else:
            status = f"exit_code={self.exit_code} duration={self.duration_s:.2f}s"
        parts = [status]
        if self.truncated:
            parts.append("(output truncated: the middle was elided)")
        if self.stdout:
            parts.append("--- stdout ---\n" + self.stdout.rstrip("\n"))
        if self.stderr:
            parts.append("--- stderr ---\n" + self.stderr.rstrip("\n"))
        if not self.stdout and not self.stderr:
            parts.append("(no output)")
        return "\n".join(parts)


class Capture:
    """Keeps the first and last `limit/2` bytes of a stream, dropping the middle."""

    def __init__(self, limit: int):
        self.half = max(limit // 2, 1024)
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def feed(self, chunk: bytes) -> None:
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.half:
                cut = len(self.tail) - self.half
                self.dropped += cut
                del self.tail[:cut]

    def text(self) -> str:
        head = self.head.decode("utf-8", errors="replace")
        tail = self.tail.decode("utf-8", errors="replace")
        if self.dropped:
            return f"{head}\n\n... [{self.dropped} bytes elided] ...\n\n{tail}"
        return head + tail


async def pump(stream: asyncio.StreamReader | None, capture: Capture) -> None:
    if stream is None:
        return
    while chunk := await stream.read(65536):
        capture.feed(chunk)


async def _feed(stream: asyncio.StreamWriter | None, data: str | None) -> None:
    if stream is None or data is None:
        return
    try:
        stream.write(data.encode())
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        # The command exited or closed stdin without reading everything; that is its choice.
        pass
    finally:
        stream.close()


class Sandbox:
    """Runs agent commands through the configured backend with timeouts, output caps and cancellation.

    Construction picks the backend, prepares the private home and an empty /tmp, and checks
    the resource limits. Raises SandboxUnavailable when the configured backend or a configured
    limit cannot work on this machine.
    """

    def __init__(self, config: Config):
        self.config = config
        self.workspace = config.workspace
        assert config.state_dir is not None
        self.home = config.state_dir / "home"
        self.tmp = config.state_dir / "tmp"
        self.backend: Backend = select(config, home=self.home, tmp=self.tmp)
        self.enabled = self.backend.confined
        self.limits = ResourceLimits(config.limits.memory_mb, config.limits.max_pids)
        # Limits scope of each spawned process, by pid, until reap() kills it.
        self._scopes: dict[int, Path | None] = {}
        if self.limits.active:
            reason = self.limits.available()
            if reason is not None:
                raise SandboxUnavailable(
                    f"cannot enforce limits.memory_mb / limits.max_pids: {reason}; unset both to run without limits"
                )
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(mode=0o700)
        self.real_home = Path.home()
        self._prepare_home()
        self.path = self._build_path()

    def _prepare_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        gitconfig = self.home / ".gitconfig"
        # The agent owns the private home; writing through a symlink it planted would land on the host.
        if gitconfig.exists() or gitconfig.is_symlink():
            return
        # Copy only the identity from the host git config; credential helpers and
        # other settings stay outside the sandbox.
        lines = ["[safe]", "\tdirectory = *", "[init]", "\tdefaultBranch = main"]
        identity = []
        for key in ("user.name", "user.email"):
            try:
                value = subprocess.run(
                    ["git", "config", "--global", key], capture_output=True, text=True, timeout=5
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                value = ""
            if value:
                identity.append(f"\t{key.split('.')[1]} = {value}")
        if not identity:
            # Without any identity git refuses to commit. A repo's own user.name/email still wins
            # over this global fallback.
            identity = ["\tname = dev-link agent", "\temail = dev-link@localhost"]
        lines += ["[user]", *identity]
        gitconfig.write_text("\n".join(lines) + "\n")

    def _build_path(self) -> str:
        """PATH for agent commands: host PATH entries that are visible to them."""
        entries: list[str] = []
        for entry in os.environ.get("PATH", "").split(":") + list(_STD_PATH):
            if not entry or entry in entries:
                continue
            p = Path(entry)
            if not p.is_dir():
                continue
            if entry in _STD_PATH or self.backend.visible(p):
                entries.append(entry)
        return ":".join(entries)

    def env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Environment for agent commands, then config vars, then `extra`.

        Sandboxed: a clean environment built from an allowlist. Unsandboxed (--no-sandbox):
        the full host environment, so commands behave exactly as in the user's own shell.
        """
        git = {
            # There is no terminal: an editor would hang until the timeout. GIT_EDITOR=true keeps
            # the default message for merges and makes a commit without a message abort at once.
            "GIT_EDITOR": "true",
            "GIT_MERGE_AUTOEDIT": "no",
            # Never discover a repository above the workspace (matters when the sandbox is off).
            "GIT_CEILING_DIRECTORIES": str(self.workspace.parent),
        }
        if not self.enabled:
            env = dict(os.environ)
            env.update(
                {"DEV_LINK_WORKSPACE": str(self.workspace), "GIT_PAGER": "cat", "PAGER": "cat", "PYTHONUNBUFFERED": "1"}
            )
            env.update(git)
            env.update(self.config.sandbox.env)
            if extra:
                env.update(extra)
            return env
        env = {
            "PATH": self.path,
            "HOME": str(self.real_home),
            "USER": os.environ.get("USER", "dev"),
            "SHELL": "/bin/bash",
            "TERM": "dumb",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "DEV_LINK_WORKSPACE": str(self.workspace),
            # Common tools check these to disable colors, pagers and prompts.
            "NO_COLOR": "1",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONUNBUFFERED": "1",
            "CI": "1",
        }
        env.update(git)
        for name in self.config.sandbox.env_passthrough:
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(self.config.sandbox.env)
        if extra:
            env.update(extra)
        return env

    def _launch(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str] | None, unit: str | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """(full argv, process environment) for starting `argv`. Raises ToolError for bad env entries.

        `unit` names the limits scope when limits are active; a fresh name is used when it is None.
        """
        for name, value in (env or {}).items():
            if not _ENV_NAME.fullmatch(name):
                raise ToolError(f"invalid environment variable name: {name!r} (use letters, digits and _)")
            if "\0" in value:
                raise ToolError(f"environment variable {name} contains a NUL byte")
        if self.backend.confined:
            # The backend sets `env` inside the confinement only; see Bubblewrap.wrap.
            full, proc_env = self.backend.wrap(argv, cwd, env or {}), self.env()
        else:
            full, proc_env = self.backend.wrap(argv, cwd, {}), self.env(env)
        if self.limits.active:
            full, proc_env = self.limits.wrap(full, proc_env, unit)
        return full, proc_env

    def _new_scope(self) -> tuple[str | None, Path | None]:
        """(unit name, cgroup directory) for the limits scope of a new command; Nones without limits."""
        if not self.limits.active:
            return None, None
        unit = self.limits.new_unit()
        return unit, self.limits.scope_dir(unit)

    def shell_argv(self, command: str) -> list[str]:
        return ["/bin/bash", "-c", command]

    def clamp_timeout(self, timeout_s: float | None) -> float:
        limits = self.config.limits
        if timeout_s is None or timeout_s <= 0:
            return limits.default_timeout_s
        return min(float(timeout_s), limits.max_timeout_s)

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        max_output_bytes: int | None = None,
    ) -> RunResult:
        """Run a command to completion inside the sandbox.

        `cwd` must already be resolved through Workspace. The process tree is killed on
        timeout or when the calling task is cancelled. Whatever the command left running in
        its process group or limits scope (`cmd &` jobs) is killed when it returns, as the
        bubblewrap PID namespace does. Raises ToolError if the program cannot be started or
        an `env` name or value is invalid.
        """
        cwd = cwd or self.workspace
        timeout = self.clamp_timeout(timeout_s)
        limit = max_output_bytes or self.config.limits.max_output_bytes
        unit, scope = self._new_scope()
        full, proc_env = self._launch(argv, cwd, env, unit)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *full,
                cwd=str(cwd),
                env=proc_env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:  # missing program, no permission, argv too long
            raise ToolError(f"cannot start {argv[0]!r}: {exc}") from exc
        out, err = Capture(limit), Capture(limit // 4)
        timed_out = False
        try:
            try:
                async with asyncio.timeout(timeout):
                    # stdin is fed alongside the output pumps: writing it first deadlocks once the
                    # command fills its stdout pipe, and would also escape the timeout.
                    await asyncio.gather(_feed(proc.stdin, stdin), pump(proc.stdout, out), pump(proc.stderr, err))
                    await proc.wait()
            except TimeoutError:
                timed_out = True
        finally:
            if proc.returncode is None:
                kill_tree(proc.pid)
                try:
                    async with asyncio.timeout(5):
                        await proc.wait()
                except TimeoutError:
                    log.warning("process %s did not exit after SIGKILL", proc.pid)
            # Under the `none` backend bash may have exited while a background job still holds
            # its output open (that is what ran into the timeout), and nothing else ends that job.
            _reap(proc.pid, scope)
        return RunResult(
            exit_code=proc.returncode,
            stdout=out.text(),
            stderr=err.text(),
            duration_s=time.monotonic() - started,
            timed_out=timed_out,
            truncated=bool(out.dropped or err.dropped),
        )

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        log_file: IO[bytes],
        env: Mapping[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        """Start a long-running sandboxed process with stdout and stderr appended to `log_file`.

        stdin stays open as a pipe so the caller can send input. The caller owns the
        process: it stops it with `kill_tree` and, once the process has exited, calls `reap`.
        """
        unit, scope = self._new_scope()
        full, proc_env = self._launch(argv, cwd, env, unit)
        try:
            proc = await asyncio.create_subprocess_exec(
                *full,
                cwd=str(cwd),
                env=proc_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:  # missing program, no permission, argv too long
            raise ToolError(f"cannot start {argv[0]!r}: {exc}") from exc
        self._scopes[proc.pid] = scope
        return proc

    def reap(self, pid: int) -> None:
        """SIGKILL whatever a spawned process left behind after it exited: the rest of its
        process group and everything in its limits scope, including setsid children.

        Call it right after the process was reaped, so its pid cannot have been reused.
        """
        _reap(pid, self._scopes.pop(pid, None))


def _reap(pid: int, scope: Path | None) -> None:
    kill_group_rest(pid)
    if scope is not None:
        kill_scope_dir(scope)


def kill_tree(pid: int, sig: int = signal.SIGKILL) -> None:
    """Signal the process group started with start_new_session=True.

    With bubblewrap, killing bwrap also kills everything in its PID namespace
    because of --die-with-parent. A direct child of `pid` that is the init of its own PID
    namespace gets SIGKILL whatever `sig` is. SIGKILL also empties the command's limits scope,
    if any.
    """
    if sig == signal.SIGKILL:
        # Before the group kill: once the process is gone its cgroup cannot be looked up.
        kill_scope(pid)
    # The kernel drops signals other than SIGKILL sent to a namespace init that has no handler,
    # and bwrap's init arms --die-with-parent only after it starts. Without this, stopping bwrap
    # in its first few ms orphans the init with the command still running.
    for child in _namespace_inits(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(child, signal.SIGKILL)
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        log.warning("no permission to signal process group %s", pid)


def kill_group_rest(pgid: int) -> None:
    """SIGKILL whatever is left in the process group `pgid` after its leader was reaped.

    Linux keeps a PID number reserved while it is still some live process's group id, so this
    reaches only stragglers of that group; an empty group is a no-op.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _namespace_inits(pid: int) -> list[int]:
    """Direct children of `pid` that are pid 1 of a nested PID namespace, read from /proc."""
    found: list[int] = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return found
    for task in tasks:
        try:
            children = Path(f"/proc/{pid}/task/{task}/children").read_text().split()
        except OSError:
            continue
        for child in children:
            try:
                status = Path(f"/proc/{child}/status").read_text()
            except OSError:
                continue
            # NSpid lists the pid in each namespace from the outermost in; "1" last means init.
            nspid = next((line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")), [])
            if len(nspid) > 1 and nspid[-1] == "1":
                found.append(int(child))
    return found
