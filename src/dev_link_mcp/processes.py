"""Background processes (dev servers, watchers, REPLs) started by the agent."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import re
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from dev_link_mcp import patterns
from dev_link_mcp.audit import AuditStore, StreamRedactor
from dev_link_mcp.sandbox import Sandbox, kill_group_rest, kill_tree

log = logging.getLogger(__name__)

# Subdirectory of the state dir that holds one log file per background process.
LOG_DIR = "processes"
# How long stop() waits for a process to exit after SIGKILL. One stuck in the kernel (D state)
# ignores even SIGKILL and must not hold up the server's shutdown.
REAP_TIMEOUT_S = 5.0
# How long an exited process's output may take to reach its log. Once its group and scope are
# killed the pipe closes at once, unless a job that escaped both still holds it.
PUMP_DRAIN_S = 1.0
# How long output that could be the start of the token is held back when no more output follows.
# A prompt such as "Enter value: " followed by a few such characters must still reach readers;
# a token only leaks if the process pauses this long in the middle of writing it.
PUMP_HOLD_S = 0.2
# Input a process has not read yet stays in server memory; past this, more input is refused.
MAX_UNREAD_INPUT = 1024 * 1024
# Least time one pattern search gets near the end of a wait, so a normal pattern is not
# reported as a runaway just because little of the wait was left.
_MIN_SEARCH_S = 0.5


class ProcessExited(ToolError):
    """A process exited nonzero while a caller was waiting for its output or port."""


class LogPump:
    """Copies a background process's stdout and stderr into its log file, with secrets redacted.

    The process writes to a pipe whose other end this class reads, so the server token never
    reaches the disk even when the process prints it. The pipe is made here rather than with
    stdout=PIPE because asyncio's Process.wait() then also waits for the pipe to close, which a
    background job (`cmd &`) that outlives bash would hold up until it exits.

    Output that could be the start of the token is held back until more arrives, but for at
    most PUMP_HOLD_S.
    """

    def __init__(self, log_path: Path, redactor: StreamRedactor):
        """Create (or append to) `log_path` and the pipe. Raises OSError if either fails."""
        self._file = log_path.open("ab", buffering=0)
        try:
            read_fd, write_fd = os.pipe()
        except OSError:
            self._file.close()
            raise
        self._read = open(read_fd, "rb", buffering=0)  # noqa: SIM115 - the pump owns it until close()
        # Give this to the process as stdout; start() closes the server's copy.
        self.process_end = open(write_fd, "wb", buffering=0)  # noqa: SIM115
        self._redactor = redactor
        self._transport: asyncio.ReadTransport | None = None
        self._task: asyncio.Task[None] | None = None
        self._write_failed = False

    async def start(self) -> None:
        """Start copying. Call once the process holds its end of the pipe."""
        self.process_end.close()
        reader = asyncio.StreamReader()
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), self._read)
        self._task = asyncio.create_task(self._copy(reader))

    async def _copy(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                # No timer while nothing is held, so an idle process costs no wakeups.
                async with asyncio.timeout(PUMP_HOLD_S if self._redactor.holding else None):
                    chunk = await reader.read(1 << 16)
            except TimeoutError:
                self._write(self._redactor.flush())
                continue
            if not chunk:
                break
            self._write(self._redactor.feed(chunk))
        self._write(self._redactor.flush())

    def _write(self, data: bytes) -> None:
        if not data or self._write_failed:
            return
        try:
            self._file.write(data)
        except OSError as exc:
            # The pipe is still read to the end, so a process on a full disk loses its log
            # rather than blocking on a full pipe.
            self._write_failed = True
            log.warning("cannot write process log %s, dropping further output: %s", self._file.name, exc)

    async def close(self, drain_s: float) -> None:
        """Let the copy run to the end of the output for up to `drain_s` seconds, then stop it
        and close the pipe and the log file. Safe to call more than once and before start().

        A job that left the process group can keep the pipe open; it gets EPIPE once the pipe
        is closed here.
        """
        try:
            if self._task is not None:
                await asyncio.wait([self._task], timeout=drain_s)
                if not self._task.done():
                    self._task.cancel()
                    await asyncio.wait([self._task])
                if not self._task.cancelled() and (exc := self._task.exception()) is not None:
                    log.error("copying process output to %s failed", self._file.name, exc_info=exc)
        finally:
            if self._task is not None:
                self._task.cancel()  # a no-op once done; stops the copy if this call was cancelled
            if self._transport is not None:
                self._transport.close()
            self._read.close()
            self.process_end.close()
            self._file.close()


@dataclass
class ManagedProcess:
    id: str
    name: str
    command: str
    cwd: str
    log_path: Path
    proc: asyncio.subprocess.Process
    pump: LogPump
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

    # Exited processes kept for list_processes and process_output; older ones are forgotten
    # (their log files stay on disk until the retention prune removes them).
    keep_exited = 50

    def __init__(self, sandbox: Sandbox, audit: AuditStore, state_dir: Path, max_processes: int):
        self.sandbox = sandbox
        self.audit = audit
        self.log_dir = state_dir / LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_processes = max_processes
        self._procs: dict[str, ManagedProcess] = {}
        self._counter = 0
        # start() calls between the limit check and registration; they count against the limit.
        self._starting = 0
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
        if len(running) + self._starting >= self.max_processes:
            raise ToolError(f"process limit reached ({self.max_processes}); stop one first")
        self._counter += 1
        pid_name = f"p{self._counter}"
        log_path = self.log_dir / f"{pid_name}-{int(time.time())}.log"
        self._starting += 1
        try:
            pump = LogPump(log_path, self.audit.stream_redactor())
            try:
                argv = self.sandbox.shell_argv(command)
                proc = await self.sandbox.spawn(argv, cwd=cwd, log_file=pump.process_end, env=env)
            except BaseException:
                await pump.close(0)
                raise
        except BaseException:
            log_path.unlink(missing_ok=True)
            raise
        finally:
            self._starting -= 1
        try:
            await pump.start()
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
                pump=pump,
                event_id=event_id,
            )
            managed.watcher = asyncio.create_task(self._watch(managed))
        except BaseException:
            # Not registered yet, so nothing else would ever stop it (an audit write can fail on a
            # full disk). The unreaped leader keeps its pid, so reap() cannot hit a reused one.
            kill_tree(proc.pid)
            self.sandbox.reap(proc.pid)
            await pump.close(0)
            raise
        self._procs[pid_name] = managed
        self._notify(managed, "started")
        self.watch_logs()
        return managed

    async def _watch(self, managed: ManagedProcess) -> None:
        code = await managed.proc.wait()
        # Without a PID namespace, `cmd &` jobs and setsid children outlive bash; end them with
        # it, as bubblewrap does, since nothing else tracks them.
        self.sandbox.reap(managed.proc.pid)
        # Before the exit is visible, so whoever sees it also finds all of the output in the log.
        await managed.pump.close(PUMP_DRAIN_S)
        managed.exit_code = code
        managed.ended = time.time()
        tail, _ = self.read_log(managed.id, tail_lines=40)
        self.audit.finish(
            managed.event_id,
            ok=code == 0 or code in (-signal.SIGTERM, -signal.SIGKILL),
            result=f"exit_code={code}\n{tail}",
        )
        self._notify(managed, "stopped" if managed.stopping else "exited")
        self._forget_old()

    def _forget_old(self) -> None:
        """Drop the processes that exited longest ago beyond `keep_exited`; running ones always stay."""
        exited = sorted((p for p in self._procs.values() if not p.running), key=lambda p: p.ended or 0.0)
        for managed in exited[: max(0, len(exited) - self.keep_exited)]:
            del self._procs[managed.id]

    def _notify(self, managed: ManagedProcess, change: str) -> None:
        live = self.audit.live
        if live.active:
            live.publish("process", {"change": change, **managed.summary()})

    def watch_logs(self) -> None:
        """Publish "log" messages while a dashboard stream is open and a process is running.

        Growth is seen by checking log file sizes twice a second, and only while both conditions
        hold, so a chatty process sends at most two messages a second. Each message is
        {"id", "offset"}, where offset is the new log size in bytes. Safe to call often.
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

    def log_paths(self) -> set[Path]:
        """Log files of every process list() shows, running or exited; nothing is read from disk."""
        return {p.log_path for p in self._procs.values()}

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

        Raises ToolError if the process is not running, closed stdin, does not read the
        input within `timeout_s` (a full pipe would otherwise block the call forever), or has
        not yet read more than MAX_UNREAD_INPUT bytes of earlier input. On a timeout the unread
        input stays queued in server memory and is delivered if the process reads later.
        """
        managed = self.get(process_id)
        if not managed.running or managed.proc.stdin is None:
            raise ToolError(f"process {process_id} is not running")
        unread = managed.proc.stdin.transport.get_write_buffer_size()
        if unread > MAX_UNREAD_INPUT:
            raise ToolError(
                f"process {process_id} has not read {unread} bytes of earlier input yet;"
                " send more once it reads them, or stop it"
            )
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
        """SIGTERM, then SIGKILL after `grace_s`. Returns the exit code.

        Returns None, with a warning logged, when the process is still there REAP_TIMEOUT_S
        after the SIGKILL (stuck in the kernel); it then stays listed as running.
        """
        managed = self.get(process_id)
        if managed.running:
            managed.stopping = True
            kill_tree(managed.proc.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(grace_s):
                    await managed.proc.wait()
            except TimeoutError:
                kill_tree(managed.proc.pid, signal.SIGKILL)
                try:
                    async with asyncio.timeout(REAP_TIMEOUT_S):
                        await managed.proc.wait()
                except TimeoutError:
                    log.warning("process %s (pid %s) did not exit after SIGKILL", process_id, managed.proc.pid)
                    return None
            # A bwrap init forked just after the SIGTERM ignores it and has no parent-death
            # signal yet, so it can outlive its bwrap; it is still in this group.
            kill_group_rest(managed.proc.pid)
        if managed.watcher:
            # Shielded: a cancelled stop must not cancel the watcher, or the exit would never be
            # recorded and the process would stay listed as running.
            await asyncio.shield(managed.watcher)
        return managed.exit_code

    async def wait_for(
        self, process_id: str, *, pattern: str | None = None, port: int | None = None, timeout_s: float = 30.0
    ) -> str:
        """Block until the log matches `pattern`, `port` accepts TCP connections, or the process exits.

        Returns a short description of what happened. Raises ToolError on timeout,
        ProcessExited when the process exits nonzero on its own while a pattern or port was
        awaited, and PatternStuck when matching `pattern` itself outlasts the timeout.
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
                # The pattern comes from the agent and may backtrack for hours; see patterns.py.
                remaining = max(deadline - time.monotonic(), _MIN_SEARCH_S)
                match = await patterns.search(regex, text, timeout_s=remaining)
                if match is not None:
                    return f"log matched {pattern!r}: {match[:200]}"
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
