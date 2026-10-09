"""Benchmark the dashboard in headless Chromium against a large seeded audit log.

Seeds a throwaway state dir with N audit events (some with large results), starts `dev-link`
from the current environment on a free port, starts a background process that prints about
1 MB/s, then opens the dashboard and records:

  - initial render: time from navigation start to the first painted activity row
  - drawer: time to open an event with a large result until its text is shown
  - process log: time to open the log viewer until log lines are shown
  - streaming window (--duration): long tasks (count, max) while about --rate tool calls per
    second run through an MCP client; first half on the Activity tab, second half with the
    process log open
  - JS heap (CDP Performance.getMetrics) after the window, before and after a forced GC
  - server CPU seconds (the server process only, from /proc) in the streaming, idle and
    hidden windows
  - requests the page made while idle (dashboard open, no activity) and while hidden

The hidden tab is simulated: headless Chromium never hides a page, so the script overrides
`document.visibilityState` and dispatches `visibilitychange`. Timers keep running, which is
the worst case for a page that ignores visibility.

Linux only (reads /proc). Needs the dev extras (Playwright and its Chromium). Example:

    uv run python scripts/bench_dashboard.py --duration 60
    uv run python scripts/bench_dashboard.py --duration 600 --json heap-run.json

Scratch files go under ~/.cache/dev-link-bench/ and are removed unless --keep is given.
Exits 1 if the server fails to start or a step fails.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from mcp import Client
from playwright.async_api import Browser, Page, async_playwright

from dev_link_mcp.audit import AuditStore

TICKS = os.sysconf("SC_CLK_TCK")
SCRATCH = Path.home() / ".cache" / "dev-link-bench"
# Matches activity rows of both the old table dashboard and the new virtual list.
ROW = "#feed tr, #feed [role=row]"
MARKER = "bench-big-marker"
LOG_NAME = "bench-log"

TOOLS = [
    ("read_file", 20), ("run_command", 18), ("search_text", 10), ("edit_file", 9), ("list_dir", 7),
    ("grep", 6), ("file_info", 5), ("process_output", 5), ("write_file", 4), ("browser_snapshot", 3),
    ("browser_screenshot", 2), ("start_process", 2), ("http_request", 2), ("task_update", 2), ("find_symbol", 1),
    ("code_outline", 1), ("checkpoint_create", 1), ("note_write", 1), ("workspace_info", 1),
]  # fmt: skip

# Prints lines of about 100 bytes: 1000 lines every 0.1 s is about 1 MB/s.
LOG_COMMAND = (
    'python3 -u -c "import sys,time\n'
    "i=0\n"
    "while True:\n"
    "    sys.stdout.write(''.join('bench-line %d %s\\n' % (i+k, 'x'*84) for k in range(LINES)))\n"
    '    sys.stdout.flush(); i+=LINES; time.sleep(0.1)"'
)

INIT_SCRIPT = """
(() => {
  const b = window.__bench = { longtasks: [], firstRow: null, firstRowPaint: null, errors: [] };
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) b.longtasks.push({ start: e.startTime, duration: e.duration });
    }).observe({ type: "longtask", buffered: true });
  } catch (_) { b.noLongtask = true; }
  const sel = ROW_SELECTOR;
  const mo = new MutationObserver(() => {
    if (b.firstRow === null && document.querySelector(sel)) {
      b.firstRow = performance.now();
      mo.disconnect();
      requestAnimationFrame(() => setTimeout(() => { b.firstRowPaint = performance.now(); }, 0));
    }
  });
  mo.observe(document, { childList: true, subtree: true });
  window.addEventListener("securitypolicyviolation", (e) => b.errors.push("CSP " + e.violatedDirective));
})();
""".replace("ROW_SELECTOR", json.dumps(ROW))

# Runs in the page: click the element `target()` returns and resolve with the ms until `ready()`
# holds and a frame was painted. Built as one source string because the page CSP forbids eval.
TIMED_CLICK = """
async () => {
  const el = (TARGET)();
  if (!el) throw new Error("element not found");
  const t0 = performance.now();
  el.click();
  const deadline = t0 + 30000;
  while (!(READY)()) {
    if (performance.now() > deadline) throw new Error("timed out waiting for the view");
    await new Promise((r) => requestAnimationFrame(r));
  }
  await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));
  return performance.now() - t0;
}
"""

FIND_BIG_ROW = f"""() => [...document.querySelectorAll({json.dumps(ROW)})]
  .find((r) => r.textContent.includes({json.dumps(MARKER)}))"""
DRAWER_READY = """() => {
  const d = document.getElementById("drawer");
  if (!d || d.hidden) return false;
  return [...d.querySelectorAll("pre")].some((p) => p.textContent.length >= 60000);
}"""
FIND_PROC_ROW = f"""() => [...document.querySelectorAll("#procs tr, #procs [role=row]")]
  .find((r) => r.textContent.includes({json.dumps(LOG_NAME)}))"""
LOG_READY = """() => {
  const log = document.getElementById("log");
  return !!log && log.textContent.includes("bench-line");
}"""
FIND_TAB = """() => document.querySelector('[data-tab="%s"]')"""
TAB_READY = """() => { const p = document.getElementById("p-%s"); return !!p && !p.hidden; }"""

HIDE = """() => {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
  Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
  document.dispatchEvent(new Event("visibilitychange"));
}"""
SHOW = """() => {
  delete document.visibilityState;
  delete document.hidden;
  document.dispatchEvent(new Event("visibilitychange"));
}"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _cpu_s(pid: int) -> float:
    """utime + stime of one process (all its threads, not its children), in seconds."""
    with open(f"/proc/{pid}/stat") as f:
        rest = f.read().rsplit(")", 1)[1].split()
    return (int(rest[11]) + int(rest[12])) / TICKS


def _big_text(i: int, size: int, as_json: bool) -> str:
    if as_json:
        row = {"path": f"src/module_{i}.py", "line": 1, "text": "lorem ipsum dolor sit amet " * 3}
        rows: list[str] = []
        total = 2
        while total < size - 200:
            row["line"] = len(rows) + 1
            piece = json.dumps(row)
            rows.append(piece)
            total += len(piece) + 1
        return "[" + ",".join(rows) + "]"
    line = "INFO  worker-3 processed batch with id={} status=ok elapsed=12ms payload=" + "z" * 40 + "\n"
    out: list[str] = []
    total = 0
    n = 0
    while total < size:
        piece = line.format(n)
        out.append(piece)
        total += len(piece)
        n += 1
    return "".join(out)[:size]


def seed(state: Path, events: int, big: int, big_bytes: int) -> None:
    """Fill `state`/audit.sqlite3 with `events` events over the last 24 h, `big` of them with large results.

    The newest event is a large read_file result whose arguments carry MARKER, so the drawer
    measurement can find its row on the first page.
    """
    AuditStore(state, secrets=[], result_bytes=big_bytes).close()
    rng = random.Random(42)  # noqa: S311 - reproducible test data, not security
    names = [n for n, _ in TOOLS]
    weights = [w for _, w in TOOLS]
    sessions = [f"{rng.getrandbits(64):016x}" for _ in range(25)]
    clients = ["claude-code 2.4.1", "codex 1.9.0", "cursor 3.2", "python-httpx/0.28.1"]
    big_every = max(1, events // max(1, big))
    big_json = _big_text(0, big_bytes, True)
    big_plain = _big_text(1, big_bytes, False)
    now = time.time()
    start = now - 86400
    rows: list[tuple[Any, ...]] = []
    for i in range(events):
        ts = start + 86400 * i / events
        last = i == events - 1
        r = rng.random()
        kind = "tool" if r < 0.92 or last else ("session" if r < 0.95 else ("process" if r < 0.99 else "server"))
        if kind == "tool":
            name = "read_file" if last else rng.choices(names, weights)[0]
        elif kind == "session":
            name = "initialize"
        elif kind == "process":
            name = f"p{rng.randint(1, 30)}"
        else:
            name = rng.choice(["start", "stop"])
        error = rng.random() < 0.07 and not last
        dur = rng.lognormvariate(3.5, 1.4)
        stateful = rng.random() < 0.6
        session = rng.choice(sessions) if stateful else None
        client = rng.choice(clients)
        args = json.dumps({"path": f"src/pkg/mod_{i % 997}.py", "limit": 200, "note": MARKER if last else "x"})
        is_big = last or (i % big_every == 0 and i // big_every < big - 1)
        if is_big:
            result = big_json if (i // big_every) % 2 == 0 or last else big_plain
        else:
            result = f"{name} ok\n" + "line of output text for the result body\n" * rng.randint(1, 40)
        err_text = f"ToolError: {name} failed: no such file src/pkg/mod_{i % 997}.py" if error else None
        rows.append(
            (
                ts,
                ts + dur / 1000,
                kind,
                name,
                session,
                client,
                args,
                "error" if error else "ok",
                dur,
                err_text or result,
                len((err_text or result).encode()),
                err_text,
            )
        )
    db = sqlite3.connect(state / "audit.sqlite3")
    with db:
        db.executemany(
            "INSERT INTO events(ts, ended, kind, name, session, client, args, status, duration_ms, result,"
            " result_bytes, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    db.close()


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


class Load:
    """Tool calls at a steady rate through one MCP client, until stopped."""

    def __init__(self, client: Client, rate: float):
        self.client = client
        self.rate = rate
        self.calls = 0
        self.errors = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _one(self, sem: asyncio.Semaphore) -> None:
        async with sem:
            try:
                r = await self.client.call_tool("list_processes", {})
                self.errors += bool(r.is_error)
            except Exception:
                self.errors += 1
            self.calls += 1

    async def _run(self) -> None:
        sem = asyncio.Semaphore(8)
        pending: set[asyncio.Task[None]] = set()
        interval = 1 / self.rate
        due = time.monotonic()
        while not self._stop.is_set():
            task = asyncio.create_task(self._one(sem))
            pending.add(task)
            task.add_done_callback(pending.discard)
            due += interval
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), max(0, due - time.monotonic()))
        if pending:
            await asyncio.gather(*pending)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task


async def _heap(page: Page) -> dict[str, float]:
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Performance.enable")

    def pick(metrics: dict[str, Any]) -> dict[str, float]:
        m = {x["name"]: x["value"] for x in metrics["metrics"]}
        return {"heap_mb": m["JSHeapUsedSize"] / 1e6, "nodes": m.get("Nodes", 0)}

    before = pick(await cdp.send("Performance.getMetrics"))
    await cdp.send("HeapProfiler.collectGarbage")
    after = pick(await cdp.send("Performance.getMetrics"))
    await cdp.detach()
    return {
        "heap_mb": round(before["heap_mb"], 1),
        "heap_after_gc_mb": round(after["heap_mb"], 1),
        "dom_nodes_after_gc": after["nodes"],
    }


async def _longtasks(page: Page, since: float) -> dict[str, Any]:
    entries = await page.evaluate("(t) => window.__bench.longtasks.filter((e) => e.start >= t)", since)
    durations = [e["duration"] for e in entries]
    return {
        "count": len(durations),
        "over_100ms": sum(d > 100 for d in durations),
        "max_ms": round(max(durations), 1) if durations else 0.0,
    }


async def _timed_click(page: Page, target: str, ready: str) -> float:
    return round(await page.evaluate(TIMED_CLICK.replace("TARGET", target).replace("READY", ready)), 1)


async def _tab(page: Page, name: str) -> None:
    await _timed_click(page, FIND_TAB % name, TAB_READY % name)


async def _measure(args: argparse.Namespace, base: str, ui: str, server_pid: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    requests: list[str] = []
    console_errors: list[str] = []
    async with Client(base + "/mcp") as client:
        command = LOG_COMMAND.replace("LINES", str(max(1, int(args.log_rate / 1000))))
        started = await client.call_tool("start_process", {"command": command, "name": LOG_NAME})
        if started.is_error:
            raise RuntimeError(f"start_process failed: {started.content}")
        process_id = "p1"
        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch()
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            await context.add_init_script(INIT_SCRIPT)
            page = await context.new_page()
            page.on("request", lambda r: requests.append(r.url))
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))

            await page.goto(ui, wait_until="load")
            await page.wait_for_function("() => window.__bench.firstRowPaint !== null", timeout=60000)
            bench = await page.evaluate("() => window.__bench")
            out["initial_render_ms"] = round(bench["firstRowPaint"], 1)
            out["initial_first_row_ms"] = round(bench["firstRow"], 1)
            await page.wait_for_timeout(1500)
            out["load_longtasks"] = await _longtasks(page, 0)

            out["drawer_open_ms"] = await _timed_click(page, FIND_BIG_ROW, DRAWER_READY)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

            await _tab(page, "processes")
            await page.wait_for_function(f"() => !!({FIND_PROC_ROW})()", timeout=15000)
            out["log_open_ms"] = await _timed_click(page, FIND_PROC_ROW, LOG_READY)
            await _tab(page, "activity")
            await page.wait_for_timeout(500)

            # Streaming window: half on Activity, half with the process log open.
            load = Load(client, args.rate)
            half = args.duration / 2
            t_page = await page.evaluate("() => performance.now()")
            cpu0, req0, t0 = _cpu_s(server_pid), len(requests), time.monotonic()
            load.start()
            await asyncio.sleep(half)
            mid = await page.evaluate("() => performance.now()")
            await _tab(page, "processes")
            await _timed_click(page, FIND_PROC_ROW, LOG_READY)
            await asyncio.sleep(half)
            await load.stop()
            elapsed = time.monotonic() - t0
            activity = await _longtasks(page, t_page)
            both = activity
            activity_only = await page.evaluate(
                "([a, b]) => window.__bench.longtasks.filter((e) => e.start >= a && e.start < b)", [t_page, mid]
            )
            durations = [e["duration"] for e in activity_only]
            out["stream"] = {
                "seconds": round(elapsed, 1),
                "tool_calls": load.calls,
                "tool_call_errors": load.errors,
                "calls_per_s": round(load.calls / elapsed, 1),
                "longtasks_activity_tab": {
                    "count": len(durations),
                    "over_100ms": sum(d > 100 for d in durations),
                    "max_ms": round(max(durations), 1) if durations else 0.0,
                },
                "longtasks_total": both,
                "server_cpu_s": round(_cpu_s(server_pid) - cpu0, 2),
                "requests": len(requests) - req0,
            }
            out["heap"] = await _heap(page)

            stopped = await client.call_tool("stop_process", {"id": process_id})
            if stopped.is_error:
                raise RuntimeError(f"stop_process failed: {stopped.content}")
            await _tab(page, "activity")
            await page.wait_for_timeout(3000)

            cpu0, req0 = _cpu_s(server_pid), len(requests)
            await asyncio.sleep(args.idle)
            out["idle"] = {
                "seconds": args.idle,
                "server_cpu_s": round(_cpu_s(server_pid) - cpu0, 3),
                "requests": len(requests) - req0,
            }

            # Hidden with the process log open, the case where an old page polled hardest.
            await _tab(page, "processes")
            await _timed_click(page, FIND_PROC_ROW, LOG_READY)
            await page.wait_for_timeout(1000)
            await page.evaluate(HIDE)
            await page.wait_for_timeout(500)
            cpu0, req0 = _cpu_s(server_pid), len(requests)
            await asyncio.sleep(args.hidden)
            hidden_urls = requests[req0:]
            out["hidden"] = {
                "seconds": args.hidden,
                "server_cpu_s": round(_cpu_s(server_pid) - cpu0, 3),
                "requests": len(hidden_urls),
                "request_paths": sorted({u.split("/api/", 1)[-1].split("?")[0] for u in hidden_urls}),
            }
            await page.evaluate(SHOW)
            await page.wait_for_timeout(1000)
            bench = await page.evaluate("() => window.__bench")
            out["page_errors"] = console_errors + bench["errors"]
            await browser.close()
    return out


def _report(result: dict[str, Any]) -> str:
    s, i, hd, hp = result["stream"], result["idle"], result["hidden"], result["heap"]
    lines = [
        f"initial render (first row painted)   {result['initial_render_ms']:>10} ms",
        f"long tasks during load               {result['load_longtasks']['count']:>10}"
        f"   max {result['load_longtasks']['max_ms']} ms",
        f"drawer open, large result            {result['drawer_open_ms']:>10} ms",
        f"process log open                     {result['log_open_ms']:>10} ms",
        f"stream: tool calls / s               {s['calls_per_s']:>10}   ({s['tool_calls']} calls in {s['seconds']} s)",
        f"stream: long tasks, Activity tab     {s['longtasks_activity_tab']['count']:>10}"
        f"   >100 ms: {s['longtasks_activity_tab']['over_100ms']}   max {s['longtasks_activity_tab']['max_ms']} ms",
        f"stream: long tasks, whole window     {s['longtasks_total']['count']:>10}"
        f"   >100 ms: {s['longtasks_total']['over_100ms']}   max {s['longtasks_total']['max_ms']} ms",
        f"stream: server CPU                   {s['server_cpu_s']:>10} s",
        f"stream: page requests                {s['requests']:>10}",
        f"JS heap after window                 {hp['heap_mb']:>10} MB   after GC {hp['heap_after_gc_mb']} MB"
        f"   DOM nodes {int(hp['dom_nodes_after_gc'])}",
        f"idle {i['seconds']:g} s: server CPU              {i['server_cpu_s']:>10} s   requests {i['requests']}",
        f"hidden {hd['seconds']:g} s: server CPU            {hd['server_cpu_s']:>10} s   requests {hd['requests']}"
        f"   {', '.join(hd['request_paths'])}",
        f"page errors                          {len(result['page_errors']):>10}",
    ]
    lines += [f"  {e[:200]}" for e in result["page_errors"][:10]]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--events", type=int, default=100_000, help="seeded audit events (default 100000)")
    p.add_argument("--big", type=int, default=500, help="seeded events with a large result (default 500)")
    p.add_argument("--big-bytes", type=int, default=256_000, help="size of a large result (default 256000)")
    p.add_argument("--duration", type=float, default=60, help="streaming window in seconds (default 60)")
    p.add_argument("--rate", type=float, default=20, help="tool calls per second while streaming (default 20)")
    p.add_argument("--log-rate", type=int, default=1_000_000, help="process output, bytes per second (default 1e6)")
    p.add_argument("--idle", type=float, default=30, help="idle window in seconds (default 30)")
    p.add_argument("--hidden", type=float, default=30, help="hidden-tab window in seconds (default 30)")
    p.add_argument("--json", type=Path, help="also write the results as JSON to this file")
    p.add_argument("--keep", action="store_true", help="keep the scratch dir (server log, audit DB)")
    p.add_argument("--startup-timeout", type=float, default=60)
    args = p.parse_args()

    exe = shutil.which("dev-link", path=str(Path(sys.executable).parent)) or shutil.which("dev-link")
    if exe is None:
        print("dev-link is not installed in this environment", file=sys.stderr)
        return 1
    SCRATCH.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="run-", dir=SCRATCH))
    workspace, state, xdg = tmp / "ws", tmp / "state", tmp / "xdg"
    workspace.mkdir()
    state.mkdir(mode=0o700)
    (xdg / "dev-link").mkdir(parents=True)
    (xdg / "dev-link" / "config.yaml").write_text("log_level: WARNING\n")
    t = time.monotonic()
    seed(state, args.events, args.big, args.big_bytes)
    print(f"seeded {args.events} events in {time.monotonic() - t:.1f}s", file=sys.stderr)

    port = _free_port()
    cmd = [exe, "serve", str(workspace), "--state-dir", str(state), "--port", str(port), "--auth", "none"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEV_LINK_")}
    env["XDG_CONFIG_HOME"] = str(xdg)
    env["DEV_LINK_AUDIT__RESULT_BYTES"] = str(args.big_bytes)
    base = f"http://127.0.0.1:{port}"
    status = 0
    log_path = tmp / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        try:
            _wait_health(base + "/health", proc, args.startup_timeout)
            result = asyncio.run(_measure(args, base, base + "/ui/", proc.pid))
            result["config"] = {k: v for k, v in vars(args).items() if k not in ("json", "keep")}
            print(_report(result))
            if args.json:
                args.json.write_text(json.dumps(result, indent=2, default=str))
        except (Exception, KeyboardInterrupt) as exc:
            print(f"bench_dashboard: {type(exc).__name__}: {exc}", file=sys.stderr)
            status = 1
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
    if status:
        print(f"--- server log ({log_path}) ---", file=sys.stderr)
        print(log_path.read_text(errors="replace")[-4000:], file=sys.stderr)
    if args.keep or status:
        print(f"scratch dir kept: {tmp}", file=sys.stderr)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
