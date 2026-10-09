"""One server instance per state dir: its lock, files, ports and process control.

A running server holds an exclusive flock on `server.lock` for its whole life and writes its
PID there. "Running" means someone holds that lock, so a PID left in the file by a crashed
server is never trusted and never signalled.
"""

from __future__ import annotations

import codecs
import contextlib
import fcntl
import json
import os
import signal
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dev_link_mcp.cli.common import CliError

LOCK_FILE = "server.lock"
SERVER_LOG = "server.log"
TUNNEL_LOG = "tunnel.log"
CONNECTION_FILE = "connection.json"

PORT_SEARCH = 100


class Lock:
    """The held `server.lock` of a running server. Python opens files non-inheritable, so
    commands and the tunnel started by the server never keep the lock alive after it exits."""

    def __init__(self, fd: int):
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, state_dir: Path, wait_s: float = 0.5) -> Lock | None:
        """Take the lock and write this PID into it, or None when another process holds it.

        Retries for `wait_s` because holder() takes the lock for a moment while it checks.
        """
        fd = os.open(state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.05)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return cls(fd)

    def release(self) -> None:
        """Clear the PID and release the lock. Safe to call twice."""
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(self._fd, 0)
        os.close(self._fd)
        self._fd = None


def _locked(fd: int) -> bool:
    """Whether another open file holds the flock; takes and drops it at once when free."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def holder(state_dir: Path) -> int | None:
    """PID of the server holding the lock in `state_dir`, or None when none runs there.

    Never creates files. Raises CliError when the lock is held but its PID cannot be read.
    """
    try:
        fd = os.open(state_dir / LOCK_FILE, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        deadline = time.monotonic() + 1
        while True:
            if not _locked(fd):
                return None
            text = os.pread(fd, 32, 0).decode(errors="replace").strip()
            # A new holder writes its PID just after taking the lock, so for a moment the file can
            # be empty or still name a dead server. Accept a PID only if that process exists and
            # the lock is still held after reading it.
            if text.isdigit() and _alive(int(text)) and _locked(fd):
                return int(text)
            if time.monotonic() >= deadline:
                raise CliError(f"{state_dir / LOCK_FILE} is locked but holds no PID")
            time.sleep(0.05)
    finally:
        os.close(fd)


def read_connection(state_dir: Path) -> dict[str, Any] | None:
    """The running server's connection.json (URLs, workspace, pid), or None if it is missing or unreadable."""
    try:
        data = json.loads((state_dir / CONNECTION_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def connection(state_dir: Path, workspace: Path) -> dict[str, Any]:
    """connection.json of a running server. Raises CliError when it is not running or not ready."""
    if holder(state_dir) is None:
        raise CliError(f"not running for {workspace}")
    info = read_connection(state_dir)
    if info is None:
        raise CliError(f"the server for {workspace} is still starting; try again in a moment")
    return info


def stop(state_dir: Path, pid: int, *, grace_s: float = 10) -> None:
    """Stop the server holding the lock: SIGTERM, then SIGKILL after `grace_s` seconds.

    A server started by `dev-link start` leads its own session, so its whole process group is
    signalled, which includes the tunnel. Any other holder (a foreground `serve`) gets the
    signal alone, so a shell pipeline it is part of is left alone. Returns once the lock is
    released; raises CliError if it still is not 5 s after SIGKILL.
    """
    try:
        group = os.getsid(pid) == pid
    except ProcessLookupError:
        return
    for sig, wait_s in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 5.0)):
        try:
            if group:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if holder(state_dir) is None:
                return
            time.sleep(0.1)
    raise CliError(f"PID {pid} still holds {state_dir / LOCK_FILE} after SIGKILL")


def port_free(host: str, port: int) -> bool:
    """Whether a server could bind `host:port` now. Uses SO_REUSEADDR like uvicorn, so
    connections in TIME_WAIT after a stop do not count as busy."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    family, kind, proto, _, address = infos[0]
    with socket.socket(family, kind, proto) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(address)
        except OSError:
            return False
    return True


def first_free_port(host: str, start: int, count: int = PORT_SEARCH) -> int | None:
    """The first port from `start` that port_free() accepts, trying `count` ports; None if all are busy."""
    return next((port for port in range(start, start + count) if 0 < port < 65536 and port_free(host, port)), None)


def tail(path: Path, lines: int) -> list[str]:
    """The last `lines` lines of a text file, without their line ends. Reads from the end, so
    large logs cost only what is printed."""
    if lines <= 0:
        return []
    with path.open("rb") as f:
        end = f.seek(0, os.SEEK_END)
        data = b""
        pos = end
        while pos > 0 and data.count(b"\n") <= lines:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return data.decode(errors="replace").splitlines()[-lines:]


def follow(path: Path, offset: int, poll_s: float = 0.25) -> Iterator[str]:
    """Text appended to `path` after byte `offset`, forever. Polls, so it works on every OS.

    A new start truncates the log in place; when the file shrinks or its first bytes change,
    it is read again from the top.
    """
    # Incremental, so a character split across two reads is decoded whole.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with path.open("rb") as f:
        head = f.read(64)
        f.seek(offset)
        while True:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = f.tell()
            if size < f.tell() or os.pread(f.fileno(), len(head), 0) != head:
                f.seek(0)
                decoder.reset()
            chunk = f.read()
            if chunk:
                head = os.pread(f.fileno(), 64, 0)
                text = decoder.decode(chunk)
                if text:
                    yield text
            else:
                time.sleep(poll_s)
