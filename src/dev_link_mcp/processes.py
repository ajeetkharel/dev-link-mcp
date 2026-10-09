"""Background processes (dev servers, watchers, REPLs) started by the agent."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import re
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from dev_link_mcp.audit import AuditStore
from dev_link_mcp.sandbox import Sandbox, kill_group_rest, kill_tree

# Subdirectory of the state dir that holds one log file per background process.
LOG_DIR = "processes"


class ProcessExited(ToolError):
    """A process exited nonzero while a caller was waiting for its output or port."""


@dataclass
class ManagedProcess:
    id: str
    name: str
    command: str
    cwd: str
    log_path: Path
    proc: asyncio.subprocess.Process
    event_id: int
    started: float = field(default_factory=time.time)
    ended: float | None = None
    exit_code: int | None = None
    watcher: asyncio.Task[None] | None = None
    stopping: bool = False

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "cwd": self.cwd,
            "pid": self.proc.pid,
            "status": "running" if self.running else f"exited({self.exit_code})",
            "started": self.started,
            "ended": self.ended,
            "uptime_s": round((self.ended or time.time()) - self.started, 1),
            "log_bytes": self.log_path.stat().st_size if self.log_path.exists() else 0,
        }


class ProcessManager:
    """Owns every background process; all of them are killed when the server stops."""

    def __init__(self, sandbox: Sandbox, audit: AuditStore, state_dir: Path, max_processes: int):
        self.sandbox = sandbox
        self.audit = audit
        self.log_dir = state_dir / LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_processes = max_processes
        self._procs: dict[str, ManagedProcess] = {}
        self._counter = 0
        self._log_watch: asyncio.Task[None] | None = None

    async def start(
        self,
        command: str,
        *,
        cwd: Path,
        name: str | None = None,
        env: dict[str, str] | None = None,
        session: str | None = None,
    ) -> ManagedProcess:
        """Start `command` under bash in the sandbox. Raises ToolError at the process limit."""
        running = [p for p in self._procs.values() if p.running]
        if len(running) >= self.max_processes:
            raise ToolError(f"process limit reached ({self.max_processes}); stop one first")
        self._counter += 1
        pid_name = f"p{self._counter}"
        log_path = self.log_dir / f"{pid_name}-{int(time.time())}.log"
        try:
            with log_path.open("ab") as log_file:
                proc = await self.sandbox.spawn(self.sandbox.shell_argv(command), cwd=cwd, log_file=log_file, env=env)
        except BaseException:
            log_path.unlink(missing_ok=True)
            raise
        event_id = self.audit.start(
            "process", name or pid_name, {"id": pid_name, "command": command, "cwd": str(cwd)}, session=session
        )
        managed = ManagedProcess(
            id=pid_name,
            name=name or pid_name,
            command=command,
            cwd=str(cwd),
            log_path=log_path,
            proc=proc,
            event_id=event_id,
        )
        managed.watcher = asyncio.create_task(self._watch(managed))
        self._procs[pid_name] = managed
        self._notify(managed, "started")
        self.watch_logs()
        return managed

    async def _watch(self, managed: ManagedProcess) -> None:
        code = await managed.proc.wait()
        managed.exit_code = code
        managed.ended = time.time()
        tail, _ = self.read_log(managed.id, tail_lines=40)
        self.audit.finish(
            managed.event_id,
            ok=code == 0 or code in (-signal.SIGTERM, -signal.SIGKILL),
            result=f"exit_code={code}\n{tail}",
        )
        self._notify(managed, "stopped" if managed.stopping else "exited")

    def _notify(self, managed: ManagedProcess, change: str) -> None:
        live = self.audit.live
        if live.active:
            live.publish("process", {"change": change, **managed.summary()})

    def watch_logs(self) -> None:
        """Publish "log" messages while a dashboard stream is open and a process is running.

        Process output goes straight from the child to its log file, so growth is only seen by
        checking file sizes: twice a second, and only while both conditions hold. Each message
        is {"id", "offset"}, where offset is the new log size in bytes. Safe to call often.
        """
        idle = self._log_watch is None or self._log_watch.done()
        if idle and self.audit.live.active and any(p.running for p in self._procs.values()):
            self._log_watch = asyncio.create_task(self._watch_logs())

    async def _watch_logs(self) -> None:
        sizes: dict[str, int] = {}
        live = self.audit.live
        while live.active:
            running = [p for p in self._procs.values() if p.running]
            if not running:
                return
            for managed in running:
                try:
                    size = managed.log_path.stat().st_size
                except OSError:
                    continue
                if sizes.get(managed.id) != size:
                    sizes[managed.id] = size
                    live.publish("log", {"id": managed.id, "offset": size})
            await asyncio.sleep(0.5)

    def get(self, process_id: str) -> ManagedProcess:
        try:
            return self._procs[process_id]
        except KeyError:
            raise ToolError(f"unknown process id {process_id!r}; use list_processes") from None

    def list(self) -> list[dict[str, Any]]:
        return [p.summary() for p in self._procs.values()]

    def read_log(
        self, process_id: str, *, offset: int = 0, max_bytes: int = 100_000, tail_lines: int | None = None
    ) -> tuple[str, int]:
        """Return (text, next_offset). `tail_lines` reads the last N lines instead of from `offset`."""
        managed = self.get(process_id)
        if not managed.log_path.exists():
            return "", 0
        size = managed.log_path.stat().st_size
        with managed.log_path.open("rb") as fh:
            if tail_lines is not None:
                start = max(0, size - max_bytes)
                fh.seek(start)
                data = fh.read()
                lines = data.decode("utf-8", errors="replace").splitlines()
                # lines[-0:] would be the whole log, not zero lines.
                return "\n".join(lines[-tail_lines:] if tail_lines > 0 else []), size
            offset = max(0, min(offset, size))
            fh.seek(offset)
            data = fh.read(max_bytes)
        # A chunk may end inside a multi-byte character; leave those bytes for the next read.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(data)
        pending = len(decoder.getstate()[0])
        return text, offset + len(data) - pending

    async def send_input(self, process_id: str, text: str, timeout_s: float = 10.0) -> None:
        """Write `text` to the process's stdin.

        Raises ToolError if the process is not running, closed stdin, or does not read the
        input within `timeout_s` (a full pipe would otherwise block the call forever). On a
        timeout the unread input stays queued and is delivered if the process reads later.
        """
        managed = self.get(process_id)
        if not managed.running or managed.proc.stdin is None:
            raise ToolError(f"process {process_id} is not running")
        try:
            managed.proc.stdin.write(text.encode())
            async with asyncio.timeout(timeout_s):
                await managed.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ToolError(f"process {process_id} closed its stdin") from exc
        except TimeoutError:
            raise ToolError(
                f"process {process_id} did not read its input within {timeout_s:g}s; the rest stays queued"
            ) from None

    async def stop(self, process_id: str, grace_s: float = 5.0) -> int | None:
        """SIGTERM, then SIGKILL after `grace_s`. Returns the exit code."""
        managed = self.get(process_id)
        if managed.running:
            managed.stopping = True
            kill_tree(managed.proc.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(grace_s):
                    await managed.proc.wait()
            except TimeoutError:
                kill_tree(managed.proc.pid, signal.SIGKILL)
                await managed.proc.wait()
            # A bwrap init forked just after the SIGTERM ignores it and has no parent-death
            # signal yet, so it can outlive its bwrap; it is still in this group.
            kill_group_rest(managed.proc.pid)
        if managed.watcher:
            await managed.watcher
        return managed.exit_code

    async def wait_for(
        self, process_id: str, *, pattern: str | None = None, port: int | None = None, timeout_s: float = 30.0
    ) -> str:
        """Block until the log matches `pattern`, `port` accepts TCP connections, or the process exits.

        Returns a short description of what happened. Raises ToolError on timeout, and
        ProcessExited when the process exits nonzero on its own while a pattern or port was awaited.
        """
        managed = self.get(process_id)
        try:
            regex = re.compile(pattern) if pattern else None
        except re.error as exc:
            raise ToolError(f"invalid pattern {pattern!r}: {exc}") from exc
        deadline = time.monotonic() + timeout_s
        port_seen = False
        while time.monotonic() < deadline:
            if regex:
                text, _ = self.read_log(process_id, tail_lines=500, max_bytes=500_000)
                # The pattern comes from the agent; a catastrophic one must not block the event loop.
                match = await asyncio.to_thread(regex.search, text)
                if match:
                    return f"log matched {pattern!r}: {match.group(0)[:200]}"
            # An exited process cannot own the port; whatever answers there is another program.
            # A process that is about to fail may still be alive on the first sighting, so the
            # port must be open on two polls in a row with the process running both times.
            if port and managed.running and _port_open(port):
                if port_seen:
                    return f"port {port} is accepting connections"
                port_seen = True
            else:
                port_seen = False
            if not managed.running:
                if managed.exit_code and not managed.stopping and (regex or port):
                    raise ProcessExited(f"process exited with code {managed.exit_code}")
                return f"process exited with code {managed.exit_code}"
            await asyncio.sleep(0.25)
        targets = [f"{pattern!r}"] if pattern else []
        if port:
            targets.append(f"port {port}")
        raise ToolError(f"timed out after {timeout_s}s waiting for {' or '.join(targets) or 'the process to exit'}")

    async def shutdown(self) -> None:
        if self._log_watch is not None:
            self._log_watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._log_watch
        # All at once, so the total stays one grace period however many processes run; the
        # server itself only gets 10 s from `dev-link stop` before SIGKILL.
        await asyncio.gather(*(self._stop_quietly(managed.id) for managed in list(self._procs.values())))

    async def _stop_quietly(self, process_id: str) -> None:
        with contextlib.suppress(Exception):
            await self.stop(process_id, grace_s=2.0)


def _port_open(port: int) -> bool:
    # Dev servers that bind "localhost" often listen only on ::1, so try both loopbacks.
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.2)
                if sock.connect_ex((host, port)) == 0:
                    return True
        except OSError:  # IPv6 disabled on this host
            continue
    return False
