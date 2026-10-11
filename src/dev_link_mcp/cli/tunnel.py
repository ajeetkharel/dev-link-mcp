"""A public tunnel run by the server as a child process: a Cloudflare quick tunnel, or ngrok on
the user's fixed domain."""

from __future__ import annotations

import json
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

INSTALL_DOCS = "https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md#install-cloudflared"
INSTALL_HINT = (
    f"cloudflared is not installed; install it (Linux: {INSTALL_DOCS}, macOS: brew install cloudflared)"
    " or start without --tunnel"
)
NGROK_INSTALL_DOCS = (
    "https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md#a-url-that-stays-the-same"
)
NGROK_INSTALL_HINT = (
    f"ngrok is not installed; install it and add your authtoken (see {NGROK_INSTALL_DOCS})"
    " or remove tunnel.ngrok_url to use a quick tunnel"
)


class Tunnel:
    """A running cloudflared or ngrok whose public URL is known."""

    def __init__(self, proc: subprocess.Popen[bytes], url: str, copier: threading.Thread, program: str):
        self.proc = proc
        self.url = url
        self._copier = copier
        self._program = program

    @classmethod
    def start(
        cls, local_url: str, log_path: Path, *, secrets: list[str], ngrok_url: str = "", timeout_s: float = 30
    ) -> Tunnel:
        """Start a tunnel for `local_url` and wait until its public URL is up.

        With `ngrok_url` empty this is a cloudflared quick tunnel on a random trycloudflare.com
        URL; otherwise ngrok on that fixed URL, using the authtoken from ngrok's own config.
        Output goes to `log_path` (truncated, 0600) through a thread that replaces each of
        `secrets` with ***: both programs can log the full URL of a request, path token
        included. The tunnel runs in its own session, so Ctrl+C and `dev-link stop` reach only
        the server, which stops the tunnel first on its way down; if the server dies without
        doing that, the orphan guard kills it. Raises CliError, with the last 100 log lines,
        when the program is missing, exits early or reports no URL within `timeout_s`.
        """
        from dev_link_mcp.audit import StreamRedactor

        program = "ngrok" if ngrok_url else "cloudflared"
        exe = shutil.which(program)
        if exe is None:
            raise CliError(NGROK_INSTALL_HINT if ngrok_url else INSTALL_HINT)
        if ngrok_url:
            # --inspect=false keeps ngrok's local inspector on port 4040 from storing requests,
            # whose paths carry the token.
            argv = [exe, "http", local_url, "--url", ngrok_url, "--log", "stdout", "--log-format", "json"]
            argv.append("--inspect=false")
        else:
            argv = [exe, "tunnel", "--no-autoupdate", "--url", local_url]
        log_file = os.fdopen(open_private(log_path), "wb", buffering=0)
        try:
            proc = subprocess.Popen(
                argv,
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
                text = log_path.read_text(errors="replace")
                url = _ngrok_ready(text, ngrok_url) if ngrok_url else _quick_url(text)
                if url:
                    return cls(proc, url, copier, program)
                if proc.poll() is not None:
                    problem = f"{program} exited with code {proc.returncode} before it printed a tunnel URL"
                    break
                if time.monotonic() >= deadline:
                    problem = f"{program} printed no tunnel URL within {timeout_s:.0f} s"
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
        return f"{self._program} exited with code {self.proc.returncode}, so {self.url} no longer works"


def _quick_url(text: str) -> str | None:
    match = _URL.search(text)
    return match.group(0) if match else None


def _ngrok_ready(text: str, url: str) -> str | None:
    """`url` once an info record of ngrok's JSON log names it, else None. The match is on the
    `url` field rather than the message text, which ngrok does not document."""
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("lvl") != "info":
            continue
        found = record.get("url")
        if isinstance(found, str) and found.rstrip("/") == url:
            return url
    return None


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
