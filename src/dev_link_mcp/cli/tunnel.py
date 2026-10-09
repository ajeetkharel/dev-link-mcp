"""A Cloudflare quick tunnel run by the server as a child process."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from dev_link_mcp.cli.common import CliError
from dev_link_mcp.cli.instance import tail
from dev_link_mcp.private import open_private

# cloudflared also logs its API host (api.trycloudflare.com) when a tunnel request fails.
_URL = re.compile(r"https://(?!api\.)[A-Za-z0-9-]+\.trycloudflare\.com")

INSTALL_HINT = (
    "cloudflared is not installed; install it (e.g. `brew install cloudflared`, or the package from Cloudflare's"
    " cloudflared downloads page) or start without --tunnel"
)


class Tunnel:
    """A running `cloudflared tunnel --url` whose public URL is known."""

    def __init__(self, proc: subprocess.Popen[bytes], url: str):
        self.proc = proc
        self.url = url

    @classmethod
    def start(cls, local_url: str, log_path: Path, timeout_s: float = 30) -> Tunnel:
        """Start cloudflared for `local_url` and wait for its trycloudflare.com URL.

        Output goes to `log_path` (truncated, 0600). The child stays in this process group, so
        a signal to the server's group reaches it too. Raises CliError, with the last 100 log
        lines, when cloudflared is missing, exits early or prints no URL within `timeout_s`.
        """
        exe = shutil.which("cloudflared")
        if exe is None:
            raise CliError(INSTALL_HINT)
        fd = open_private(log_path)
        try:
            proc = subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "--url", local_url],
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=subprocess.STDOUT,
            )
        finally:
            os.close(fd)
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                match = _URL.search(log_path.read_text(errors="replace"))
                if match:
                    return cls(proc, match.group(0))
                if proc.poll() is not None:
                    problem = f"cloudflared exited with code {proc.returncode} before it printed a tunnel URL"
                    break
                if time.monotonic() >= deadline:
                    problem = f"cloudflared printed no tunnel URL within {timeout_s:.0f} s"
                    break
                time.sleep(0.25)
        except BaseException:
            _terminate(proc)
            raise
        _terminate(proc)
        raise CliError(f"{problem}; last lines of {log_path}:", details=tail(log_path, 100))

    def stop(self) -> None:
        """SIGTERM cloudflared, then SIGKILL after 5 s. Safe to call twice."""
        _terminate(self.proc)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
