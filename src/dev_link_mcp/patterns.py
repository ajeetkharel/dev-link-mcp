"""Regex work on patterns the agent wrote, in a worker process that is killed at a deadline.

CPython's `re` holds the GIL for the whole match, so a pattern that backtracks
catastrophically (`^(a+)+$` on a long non-matching line can run for hours) would freeze the
event loop and every tool with it, and no thread can be interrupted. The matching therefore
runs in one long-lived child Python process; when a request outlives its time limit the child
is killed, and the next request starts a fresh one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

__all__ = ["PatternStuck", "search", "subn"]

# Runs with `python -I`: stdlib only, JSON lines in and out (ASCII-escaped, so the locale's
# encoding does not matter). The parent death signal stops a worker stuck in a match when the
# server is killed, since it would never read the EOF on stdin.
_WORKER = r"""
import json, re, sys
try:
    import ctypes
    ctypes.CDLL(None).prctl(1, 9)  # PR_SET_PDEATHSIG, SIGKILL; Linux only
except Exception:
    pass

def run(req):
    regex = re.compile(req["pattern"], req["flags"])
    if req["op"] == "search":
        m = regex.search(req["text"])
        return None if m is None else m.group(0)
    repl = req["replacement"]
    if req["literal"]:
        repl = lambda _m, text=repl: text
    out = []
    for text in req["texts"]:
        new, count = regex.subn(repl, text)
        # Unchanged texts are not sent back; the caller still has them.
        out.append([new if count else None, count])
    return out

for line in sys.stdin:
    try:
        reply = {"ok": run(json.loads(line))}
    except re.error as exc:
        reply = {"error": "re.error", "message": str(exc)}
    except Exception as exc:
        reply = {"error": type(exc).__name__, "message": str(exc)}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
"""


class PatternStuck(ToolError):
    """A regex did not finish within its time limit and was stopped."""


class _Job:
    """One request on its way through the executor; `abandoned` is set when its caller gives up."""

    def __init__(self, request: dict[str, Any], deadline: float) -> None:
        self.request = request
        self.deadline = deadline
        self.abandoned = False


class _Worker:
    """The child process and its pipes.

    `call` runs only on the single `_executor` thread. `abandon` runs on the event loop and
    kills the worker when its job's caller was cancelled or timed out, so the thread is not
    stuck until the job's own deadline and later jobs do not queue behind it.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._job: _Job | None = None

    def abandon(self, job: _Job) -> None:
        with self._lock:
            job.abandoned = True
            proc = self.proc if self._job is job else None
        if proc is not None:
            # The thread's read then sees EOF at once and cleans up.
            with contextlib.suppress(OSError):
                proc.kill()

    def call(self, job: _Job) -> dict[str, Any]:
        """Send one request and return the decoded reply. Raises TimeoutError (worker killed) past
        the job's deadline or once it is abandoned."""
        with self._lock:
            if job.abandoned or time.monotonic() >= job.deadline:
                raise TimeoutError
            self._job = job
        try:
            return self._call(job.request, job.deadline)
        except OSError:
            if job.abandoned:
                raise TimeoutError from None
            raise
        finally:
            with self._lock:
                self._job = None

    def _call(self, request: dict[str, Any], deadline: float) -> dict[str, Any]:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _WORKER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                # A Ctrl+C meant for the server must not kill the worker in the middle of a reply.
                start_new_session=True,
            )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        try:
            # The idle worker reads all of its stdin, so this write does not block for long.
            self.proc.stdin.write((json.dumps(request) + "\n").encode())
            self.proc.stdin.flush()
            line = self._read_line(self.proc.stdout.fileno(), deadline)
        except TimeoutError:
            self._kill()
            raise
        except OSError:  # the worker died or was killed by abandon(); the next call starts a new one
            self._kill()
            raise
        reply: dict[str, Any] = json.loads(line)
        return reply

    @staticmethod
    def _read_line(fd: int, deadline: float) -> bytes:
        data = bytearray()
        while not data.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise BrokenPipeError("the regex worker exited")
            data += chunk
        return bytes(data)

    def _kill(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(OSError):
            self.proc.kill()
        self.proc.wait()
        for pipe in (self.proc.stdin, self.proc.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        self.proc = None


_worker = _Worker()
# One thread, so requests reach the worker one at a time; each is bounded by its own deadline.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dev-link-regex")


async def _run(request: dict[str, Any], timeout_s: float) -> Any:
    job = _Job(request, time.monotonic() + timeout_s)
    loop = asyncio.get_running_loop()
    try:
        # The outer timeout covers time spent queued behind another request.
        async with asyncio.timeout(timeout_s):
            reply = await loop.run_in_executor(_executor, _worker.call, job)
    except TimeoutError:  # before OSError, which it subclasses
        _worker.abandon(job)
        raise PatternStuck(
            f"the regex did not finish within {timeout_s:g}s and was stopped; it is probably backtracking"
            " catastrophically (nested quantifiers like (a+)+ or (.*)*); use a simpler pattern"
        ) from None
    except OSError as exc:
        raise ToolError(f"the regex worker failed: {exc}") from exc
    except BaseException:
        # Cancelled: stop the work now rather than at the job's deadline.
        _worker.abandon(job)
        raise
    if "ok" in reply:
        return reply["ok"]
    if reply["error"] == "IndexError":
        raise IndexError(reply["message"])
    if reply["error"] == "re.error":
        raise re.error(reply["message"])
    raise ToolError(f"regex failed: {reply['error']}: {reply['message']}")


async def search(regex: re.Pattern[str], text: str, *, timeout_s: float) -> str | None:
    """Text of the first match of `regex` in `text`, or None when there is none.

    Raises PatternStuck when matching takes longer than `timeout_s` seconds, including time
    queued behind other regex work.
    """
    request = {"op": "search", "pattern": regex.pattern, "flags": regex.flags, "text": text}
    result: str | None = await _run(request, timeout_s)
    return result


async def subn(
    regex: re.Pattern[str], replacement: str, texts: list[str], *, literal: bool, timeout_s: float
) -> list[tuple[str, int]]:
    """`regex.subn(replacement, text)` for each of `texts`, as (new_text, count) in the same order.

    `literal` inserts `replacement` as is, without backslash escapes or group references.
    Raises re.error or IndexError for a bad replacement template, as `re` does, and
    PatternStuck when all of it takes longer than `timeout_s` seconds.
    """
    request = {
        "op": "subn",
        "pattern": regex.pattern,
        "flags": regex.flags,
        "replacement": replacement,
        "literal": literal,
        "texts": texts,
    }
    result: list[list[Any]] = await _run(request, timeout_s)
    return [(text if new is None else new, count) for text, (new, count) in zip(texts, result, strict=True)]
