"""Measure the memory and CPU of a running dev-link server and the processes it starts.

Starts `dev-link` from the current environment on a throwaway workspace and state dir, then
reports, for each phase, the RSS of the server's process tree grouped as python / Playwright
driver / Chromium / other, and the CPU seconds the tree used during the phase:

  a. right after start
  b. after an idle wait with no client connected
  c. after a browser workload (browser_open about:blank, browser_snapshot, browser_screenshot)
  d. after the browser idle timeout has passed
  e. after one more browser call

Linux only (reads /proc). Example quick run, about 1.5 minutes:

    uv run python scripts/measure_resources.py --idle 60 --idle-timeout 15

Exits 1 if the server fails to start or a tool call fails.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from mcp import Client

GROUPS = ("python", "driver", "chromium", "other")
TICKS = os.sysconf("SC_CLK_TCK")


@dataclass
class Sample:
    rss_mb: dict[str, float]
    pss_mb: float
    procs: dict[str, int]
    pids: set[int]


@dataclass
class Tree:
    """Walks one root process's tree and keeps per-pid CPU so exited processes still count."""

    root: int
    cpu: dict[tuple[int, int], float] = field(default_factory=dict)

    def pids(self) -> list[int]:
        found, todo = [], [self.root]
        while todo:
            pid = todo.pop()
            found.append(pid)
            for task in _listdir(f"/proc/{pid}/task"):
                text = _read(f"/proc/{pid}/task/{task}/children")
                todo.extend(int(c) for c in text.split())
        return found

    def sample(self) -> Sample:
        rss = dict.fromkeys(GROUPS, 0.0)
        procs = dict.fromkeys(GROUPS, 0)
        pss = 0.0
        live = set()
        for pid in self.pids():
            stat = _read(f"/proc/{pid}/stat")
            if not stat:
                continue
            # Fields after the ")" of the command name: state is index 0, utime 11, stime 12, starttime 19.
            rest = stat.rsplit(")", 1)[1].split()
            self.cpu[(pid, int(rest[19]))] = (int(rest[11]) + int(rest[12])) / TICKS
            group = _classify(pid)
            rss[group] += _kb(_read(f"/proc/{pid}/status"), "VmRSS:") / 1024
            pss += _kb(_read(f"/proc/{pid}/smaps_rollup"), "Pss:") / 1024
            procs[group] += 1
            live.add(pid)
        return Sample(rss, pss, procs, live)

    def cpu_total(self) -> float:
        return sum(self.cpu.values())

    def wakeups(self) -> int:
        """Voluntary context switches of the server process, all threads: how often it woke up."""
        total = 0
        for task in _listdir(f"/proc/{self.root}/task"):
            total += _kb(_read(f"/proc/{self.root}/task/{task}/status"), "voluntary_ctxt_switches:")
        return total


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _kb(text: str, key: str) -> int:
    for line in text.splitlines():
        if line.startswith(key):
            return int(line.split()[1])
    return 0


def _starttime(pid: int) -> int | None:
    stat = _read(f"/proc/{pid}/stat")
    return int(stat.rsplit(")", 1)[1].split()[19]) if stat else None


def _classify(pid: int) -> str:
    # Names the process gave itself can mislead (the driver's comm is "MainThread"), so use argv[0].
    program = _read(f"/proc/{pid}/cmdline").split("\0")[0]
    comm = _read(f"/proc/{pid}/comm").strip()
    if "chrom" in program or "headless_shell" in program:
        return "chromium"
    if "playwright" in program and Path(program).name == "node":
        return "driver"
    if comm.startswith("python") or comm == "dev-link":
        return "python"
    return "other"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(url: str, proc: subprocess.Popen[bytes], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited with status {proc.returncode} before it was ready")
        with contextlib.suppress(OSError), urllib.request.urlopen(url, timeout=1) as resp:  # noqa: S310 - loopback URL
            if resp.status == 200:
                return
        time.sleep(0.1)
    raise RuntimeError(f"server did not answer {url} within {timeout_s:g}s")


async def _call(client: Client, name: str, args: dict[str, object]) -> object:
    result = await client.call_tool(name, args)
    if result.is_error:
        raise RuntimeError(f"{name} failed: {_text(result)}")
    return result


def _text(result: object) -> str:
    return " ".join(getattr(b, "text", "") for b in getattr(result, "content", []))


async def _sleep_sampling(tree: Tree, seconds: float) -> None:
    """Sleep while sampling once a second, so CPU of processes that exit in between is counted."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        await asyncio.sleep(min(1.0, left))
        tree.sample()


def _row(label: str, sample: Sample, cpu_s: float, wakeups_per_s: float) -> str:
    cells = [f"{sample.rss_mb[g]:7.1f} ({sample.procs[g]})" for g in GROUPS]
    total = sum(sample.rss_mb.values())
    return (
        f"{label:<34} "
        + " ".join(f"{c:>12}" for c in cells)
        + f" {total:9.1f} {sample.pss_mb:9.1f} {cpu_s:8.2f} {wakeups_per_s:9.1f}"
    )


async def _run(args: argparse.Namespace, tree: Tree, base: str, notes: list[str]) -> list[str]:
    rows = []
    mark = (tree.cpu_total(), tree.wakeups(), time.monotonic())

    def record(label: str) -> None:
        nonlocal mark
        sample = tree.sample()
        now = (tree.cpu_total(), tree.wakeups(), time.monotonic())
        rows.append(_row(label, sample, now[0] - mark[0], (now[1] - mark[1]) / (now[2] - mark[2])))
        mark = now

    record("a. right after start")
    await _sleep_sampling(tree, args.idle)
    record(f"b. after {args.idle:g}s idle, no client")
    async with Client(base + "/mcp") as client:
        opened = await _call(client, "browser_open", {"url": "about:blank"})
        page_id = getattr(opened, "structured_content", {})["page_id"]
        await _call(client, "browser_snapshot", {"page_id": page_id})
        await _call(client, "browser_screenshot", {"page_id": page_id})
        record("c. after browser workload")
        wait = args.idle_wait if args.idle_wait is not None else args.idle_timeout + 5
        await _sleep_sampling(tree, wait)
        record(f"d. after {wait:g}s without browser use")
        stale = await client.call_tool("browser_snapshot", {"page_id": page_id})
        notes.append(f"snapshot of {page_id} after the wait: error={stale.is_error} {_text(stale)[:160]!r}")
        await _call(client, "browser_open", {"url": "about:blank"})
        record("e. after one more browser_open")
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--idle", type=float, default=60, help="seconds of idle with no client for phase b (default 60)")
    p.add_argument(
        "--idle-timeout",
        type=float,
        default=15,
        help="groups.browser.idle_timeout_s written to the server config; a negative value leaves it unset"
        " (for servers without the setting). Default 15",
    )
    p.add_argument("--idle-wait", type=float, help="seconds to wait for phase d (default idle timeout + 5)")
    p.add_argument("--config", type=Path, help="extra config file passed to dev-link with --config")
    p.add_argument("--startup-timeout", type=float, default=30)
    args = p.parse_args()
    if args.idle_timeout < 0 and args.idle_wait is None:
        p.error("--idle-wait is required when --idle-timeout is negative")

    exe = shutil.which("dev-link", path=str(Path(sys.executable).parent)) or shutil.which("dev-link")
    if exe is None:
        print("dev-link is not installed in this environment", file=sys.stderr)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="dev-link-measure-"))
    workspace, state, xdg = tmp / "ws", tmp / "state", tmp / "xdg"
    workspace.mkdir()
    (xdg / "dev-link").mkdir(parents=True)
    # The generated file stands in for the user file, so the developer's own config is never read.
    user_config = "log_level: WARNING\n"
    if args.idle_timeout >= 0:
        user_config += f"groups:\n  browser:\n    idle_timeout_s: {args.idle_timeout}\n"
    (xdg / "dev-link" / "config.yaml").write_text(user_config)
    port = _free_port()
    cmd = [exe, "serve", str(workspace), "--state-dir", str(state), "--port", str(port), "--auth", "none"]
    if args.config:
        cmd += ["--config", str(args.config)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEV_LINK_")}
    env["XDG_CONFIG_HOME"] = str(xdg)
    log_path = tmp / "server.log"
    base = f"http://127.0.0.1:{port}"
    notes: list[str] = []
    status = 0
    with log_path.open("wb") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        tree = Tree(proc.pid)
        try:
            started = time.monotonic()
            _wait_health(base + "/health", proc, args.startup_timeout)
            notes.append(f"server ready in {time.monotonic() - started:.2f}s (pid {proc.pid})")
            rows = asyncio.run(_run(args, tree, base, notes))
            header = (
                f"{'phase':<34} "
                + " ".join(f"{g:>12}" for g in GROUPS)
                + f" {'RSS MB':>9} {'PSS MB':>9} {'CPU s':>8} {'py wake/s':>9}"
            )
            print("RSS in MB per process group (process count). CPU s: whole tree during the phase.")
            print("py wake/s: voluntary context switches per second of the server process during the phase.")
            print(header)
            print("-" * len(header))
            print("\n".join(rows))
        except (Exception, KeyboardInterrupt) as exc:
            for leaf in _leaves(exc):
                print(f"measure_resources: {type(leaf).__name__}: {leaf}", file=sys.stderr)
            status = 1
        finally:
            _stop(proc, tree, notes)
    if status:
        print(f"--- server log ({log_path}) ---", file=sys.stderr)
        print(log_path.read_text(errors="replace")[-4000:], file=sys.stderr)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    for note in notes:
        print(f"note: {note}")
    return status


def _leaves(exc: BaseException) -> list[BaseException]:
    """The real errors inside the exception groups the MCP client raises."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _stop(proc: subprocess.Popen[bytes], tree: Tree, notes: list[str]) -> None:
    """SIGINT the server (it shuts its groups down), then SIGKILL whatever of its tree is left."""
    children = {(pid, _starttime(pid)) for pid in tree.pids() if pid != proc.pid} if proc.poll() is None else set()
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            notes.append("server did not stop within 15s of SIGINT; killed")
    # The start time guards against signalling an unrelated process that reused a pid.
    leftovers = [pid for pid, start in children if _starttime(pid) == start]
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    for pid in leftovers:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    if leftovers:
        notes.append(f"{len(leftovers)} child processes outlived the server stop and were killed")
    proc.wait()


if __name__ == "__main__":
    sys.exit(main())
