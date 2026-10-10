"""A Cloudflare quick tunnel run by the server as a child process."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING

from dev_link_mcp import orphans
from dev_link_mcp.cli.common import CliError
from dev_link_mcp.cli.instance import tail
from dev_link_mcp.private import open_private

if TYPE_CHECKING:
    from dev_link_mcp.audit import StreamRedactor

# cloudflared also logs its API host (api.trycloudflare.com) when a tunnel request fails.
_URL = re.compile(r"https://(?!api\.)[A-Za-z0-9-]+\.trycloudflare\.com")

INSTALL_HINT = (
    "cloudflared is not installed; install it (e.g. `brew install cloudflared`, or the package from Cloudflare's"
    " cloudflared downloads page) or start without --tunnel"
)


class Tunnel:
    """A running `cloudflared tunnel --url` whose public URL is known."""

    def __init__(self, proc: subprocess.Popen[bytes], url: str, copier: threading.Thread):
        self.proc = proc
        self.url = url
        self._copier = copier

    @classmethod
    def start(cls, local_url: str, log_path: Path, *, secrets: list[str], timeout_s: float = 30) -> Tunnel:
        """Start cloudflared for `local_url` and wait for its trycloudflare.com URL.

        Output goes to `log_path` (truncated, 0600) through a thread that replaces each of
        `secrets` with ***: cloudflared logs the full URL of a request it cannot pass on, path
        token included. cloudflared runs in its own session, so Ctrl+C and `dev-link stop`
        reach only the server, which stops the tunnel first on its way down; if the server dies
        without doing that, the orphan guard kills it. Raises CliError, with the last 100 log
        lines, when cloudflared is missing, exits early or prints no URL within `timeout_s`.
        """
        from dev_link_mcp.audit import StreamRedactor

        exe = shutil.which("cloudflared")
        if exe is None:
            raise CliError(INSTALL_HINT)
        log_file = os.fdopen(open_private(log_path), "wb", buffering=0)
        try:
            proc = subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "--url", local_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            log_file.close()
            raise
        orphans.watch(proc.pid)
        assert proc.stdout is not None
        copier = threading.Thread(
            target=_copy, args=(proc.stdout, log_file, StreamRedactor(secrets)), name="tunnel log", daemon=True
        )
        copier.start()
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                match = _URL.search(log_path.read_text(errors="replace"))
                if match:
                    return cls(proc, match.group(0), copier)
                if proc.poll() is not None:
                    problem = f"cloudflared exited with code {proc.returncode} before it printed a tunnel URL"
                    break
                if time.monotonic() >= deadline:
                    problem = f"cloudflared printed no tunnel URL within {timeout_s:.0f} s"
                    break
                time.sleep(0.25)
        except BaseException:
            _terminate(proc, copier)
            raise
        _terminate(proc, copier)
        raise CliError(f"{problem}; last lines of {log_path}:", details=tail(log_path, 100))

    def stop(self) -> None:
        """SIGTERM cloudflared, then SIGKILL after 5 s, and wait for the rest of its output to
        reach the log. Safe to call twice."""
        _terminate(self.proc, self._copier)

    def failure(self) -> str | None:
        """None while cloudflared runs, else why the public URL stopped working. Cheap enough to
        call every second."""
        if self.proc.poll() is None:
            return None
        orphans.forget(self.proc.pid)
        return f"cloudflared exited with code {self.proc.returncode}, so {self.url} no longer works"


def _terminate(proc: subprocess.Popen[bytes], copier: threading.Thread) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    orphans.forget(proc.pid)
    # cloudflared starts no children, so its exit closes the pipe and the copy ends right away.
    copier.join(5)


def _copy(source: IO[bytes], log_file: IO[bytes], redactor: StreamRedactor) -> None:
    """Copy cloudflared's output into the log until it closes, redacted; closes both."""
    failed = False
    with source, log_file:
        while True:
            chunk = os.read(source.fileno(), 1 << 16)
            data = redactor.feed(chunk) if chunk else redactor.flush()
            if data and not failed:
                try:
                    log_file.write(data)
                except OSError:
                    # Keep reading, or cloudflared would block on a full pipe; the log just stops.
                    failed = True
            if not chunk:
                return
