import asyncio
import contextlib
import logging
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from dev_link_mcp.audit import AuditStore, StreamRedactor
from dev_link_mcp.config import Config
from dev_link_mcp.patterns import PatternStuck
from dev_link_mcp.processes import LogPump, ProcessManager, _port_open
from dev_link_mcp.sandbox import Sandbox

# Leftover processes are found by scanning /proc, which only Linux has.
needs_proc = pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to find leftover processes")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_log(pm: ProcessManager, pid: str, needle: str, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text, _ = pm.read_log(pid)
        if needle in text:
            return text
        await asyncio.sleep(0.05)
    raise AssertionError(f"{needle!r} not in log: {pm.read_log(pid)[0]!r}")


@pytest.fixture
def pm(rt):
    return rt.processes


async def test_start_runs_in_workspace_and_logs(pm, rt, workspace):
    proc = await pm.start("pwd; echo to-stderr >&2; echo done", cwd=workspace, name="hello")
    assert proc.id == "p1" and proc.name == "hello" and proc.running
    await pm.wait_for(proc.id, timeout_s=10)
    await proc.watcher
    assert proc.exit_code == 0 and not proc.running
    text, offset = pm.read_log(proc.id)
    assert text == f"{workspace}\nto-stderr\ndone\n"
    assert offset == len(text.encode())
    assert proc.log_path.parent == rt.config.state_dir / "processes"
    summary = pm.list()[0]
    assert summary["id"] == "p1" and summary["status"] == "exited(0)" and summary["log_bytes"] == offset
    event = rt.audit.get(proc.event_id)
    assert event["kind"] == "process" and event["name"] == "hello" and event["status"] == "ok"
    assert "exit_code=0" in event["result"] and "done" in event["result"]


async def test_failed_process_is_audited_as_error(pm, rt, workspace):
    proc = await pm.start("exit 4", cwd=workspace)
    assert await pm.wait_for(proc.id, timeout_s=10) == "process exited with code 4"
    await proc.watcher
    assert rt.audit.get(proc.event_id)["status"] == "error"
    assert proc.name == proc.id


async def test_read_log_offsets_and_tail(pm, workspace):
    proc = await pm.start("for i in $(seq 1 50); do echo line-$i; done", cwd=workspace)
    await pm.wait_for(proc.id, timeout_s=10)
    await proc.watcher
    full, end = pm.read_log(proc.id)
    first, next_offset = pm.read_log(proc.id, max_bytes=20)
    assert first == full[:20] and next_offset == 20
    rest, end2 = pm.read_log(proc.id, offset=next_offset)
    assert first + rest == full and end2 == end
    assert pm.read_log(proc.id, offset=end) == ("", end)
    assert pm.read_log(proc.id, offset=end + 100) == ("", end)  # past the end is clamped
    assert pm.read_log(proc.id, offset=-5)[0] == full
    tail, tail_offset = pm.read_log(proc.id, tail_lines=3)
    assert tail == "line-48\nline-49\nline-50" and tail_offset == end
    assert pm.read_log(proc.id, tail_lines=0) == ("", end)
    assert pm.read_log(proc.id, tail_lines=1000)[0].splitlines()[0] == "line-1"


async def test_read_log_appends_while_running(pm, workspace):
    proc = await pm.start("echo one; read x; echo two-$x; sleep 3060", cwd=workspace)
    await _wait_log(pm, proc.id, "one\n")
    _, offset = pm.read_log(proc.id)
    await pm.send_input(proc.id, "abc\n")
    await _wait_log(pm, proc.id, "two-abc")
    new, _ = pm.read_log(proc.id, offset=offset)
    assert new == "two-abc\n"
    await pm.stop(proc.id)


async def test_send_input_to_exited_process(pm, workspace):
    proc = await pm.start("true", cwd=workspace)
    await pm.wait_for(proc.id, timeout_s=10)
    await proc.watcher
    with pytest.raises(ToolError, match="not running"):
        await pm.send_input(proc.id, "x")


async def test_unknown_id(pm):
    with pytest.raises(ToolError, match="unknown process id"):
        pm.get("p99")
    with pytest.raises(ToolError, match="unknown process id"):
        pm.read_log("nope")
    with pytest.raises(ToolError, match="unknown process id"):
        await pm.stop("nope")


async def test_wait_for_pattern(pm, workspace):
    proc = await pm.start("sleep 0.3; echo 'Server listening on :1234'; sleep 3061", cwd=workspace)
    result = await pm.wait_for(proc.id, pattern=r"listening on :\d+", timeout_s=10)
    assert result == "log matched 'listening on :\\\\d+': listening on :1234"
    await pm.stop(proc.id)


async def test_wait_for_invalid_regex_is_tool_error(pm, workspace):
    proc = await pm.start("sleep 3062", cwd=workspace)
    with pytest.raises(ToolError, match="invalid pattern"):
        await pm.wait_for(proc.id, pattern="(unclosed", timeout_s=1)
    await pm.stop(proc.id)


async def test_wait_for_port(pm, workspace):
    port = _free_port()
    proc = await pm.start(f"sleep 0.3; exec python3 -m http.server {port} --bind 127.0.0.1", cwd=workspace)
    result = await pm.wait_for(proc.id, port=port, timeout_s=15)
    assert result == f"port {port} is accepting connections"
    await pm.stop(proc.id)


async def test_wait_for_exit_and_timeout(pm, workspace):
    proc = await pm.start("sleep 3063", cwd=workspace)
    started = time.monotonic()
    with pytest.raises(ToolError, match=r"timed out after 0\.5s waiting for 'never'"):
        await pm.wait_for(proc.id, pattern="never", timeout_s=0.5)
    assert time.monotonic() - started < 3
    with pytest.raises(ToolError, match=r"timed out after 0\.5s waiting for the process to exit"):
        await pm.wait_for(proc.id, timeout_s=0.5)
    await pm.stop(proc.id)
    assert "exited" in await pm.wait_for(proc.id, pattern="never", timeout_s=1)


async def test_stop_sandboxed_process(pm, rt, workspace):
    proc = await pm.start("sleep 3064", cwd=workspace)
    code = await pm.stop(proc.id, grace_s=5)
    assert code is not None and not proc.running
    assert proc.watcher.done()
    assert rt.audit.get(proc.event_id)["status"] == "ok"  # stopped by us, not a failure
    assert await pm.stop(proc.id) == code  # idempotent


async def test_stop_escalates_to_sigkill(tmp_path, workspace):
    # Unsandboxed so SIGTERM reaches bash directly; bwrap would die on TERM and never need the escalation.
    config = Config(workspace=workspace, state_dir=tmp_path / "state2").finalize()
    config.sandbox.backend = "none"
    audit = AuditStore(config.state_dir, secrets=[], result_bytes=1000)
    pm = ProcessManager(Sandbox(config), audit, config.state_dir, max_processes=4)
    try:
        proc = await pm.start("trap '' TERM; echo ready; while true; do sleep 0.1; done", cwd=workspace)
        await _wait_log(pm, proc.id, "ready")
        started = time.monotonic()
        code = await pm.stop(proc.id, grace_s=0.5)
        assert code == -signal.SIGKILL
        assert 0.4 < time.monotonic() - started < 5
        assert audit.get(proc.event_id)["status"] == "ok"
    finally:
        await pm.shutdown()
        audit.close()


async def test_shutdown_stops_processes_at_the_same_time(tmp_path, workspace):
    # Each one ignores SIGTERM and waits out the whole grace period; one after another, the
    # shutdown would outlast the 10 s that `dev-link stop` gives the server.
    config = Config(workspace=workspace, state_dir=tmp_path / "state3").finalize()
    config.sandbox.backend = "none"
    audit = AuditStore(config.state_dir, secrets=[], result_bytes=1000)
    pm = ProcessManager(Sandbox(config), audit, config.state_dir, max_processes=8)
    try:
        procs = [
            await pm.start("trap '' TERM; echo ready; while true; do sleep 0.1; done", cwd=workspace) for _ in range(4)
        ]
        for proc in procs:
            await _wait_log(pm, proc.id, "ready")
        started = time.monotonic()
        await pm.shutdown()
        elapsed = time.monotonic() - started
        assert all(p.proc.returncode == -signal.SIGKILL for p in procs)
        assert 1.5 < elapsed < 4, elapsed
    finally:
        await pm.shutdown()
        audit.close()


async def test_process_limit(tmp_path, workspace, rt):
    pm = ProcessManager(rt.sandbox, rt.audit, tmp_path / "pm-state", max_processes=2)
    try:
        a = await pm.start("sleep 3065", cwd=workspace)
        await pm.start("sleep 3065", cwd=workspace)
        with pytest.raises(ToolError, match="process limit reached"):
            await pm.start("sleep 3065", cwd=workspace)
        await pm.stop(a.id)
        third = await pm.start("sleep 3065", cwd=workspace)  # stopped ones free a slot
        assert third.id == "p3"
    finally:
        await pm.shutdown()


async def test_env_is_passed(pm, workspace):
    proc = await pm.start('echo "v=$MY_VAR"', cwd=workspace, env={"MY_VAR": "42"})
    await pm.wait_for(proc.id, timeout_s=10)
    await proc.watcher
    assert pm.read_log(proc.id)[0] == "v=42\n"


@needs_proc
async def test_shutdown_kills_all(pm, workspace):
    procs = [await pm.start(f"sleep 307{i}", cwd=workspace) for i in range(3)]
    await asyncio.sleep(0.2)
    await pm.shutdown()
    assert all(not p.running for p in procs)
    for p in procs:
        assert p.proc.returncode is not None
    await asyncio.sleep(0.2)
    alive = [
        e
        for e in Path("/proc").iterdir()
        if e.name.isdigit() and _cmdline(e).startswith(("sleep 3070", "sleep 3071", "sleep 3072"))
    ]
    assert not alive


def _cmdline(entry: Path) -> str:
    try:
        return entry.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
    except OSError:
        return ""


async def test_send_input_to_process_not_reading_times_out(pm, workspace):
    proc = await pm.start("sleep 3066", cwd=workspace)
    started = time.monotonic()
    # Far more than a pipe buffer holds, so the write cannot complete until something reads.
    with pytest.raises(ToolError, match=r"did not read its input within 0\.5s"):
        await pm.send_input(proc.id, "x" * 2_000_000, timeout_s=0.5)
    assert time.monotonic() - started < 5
    await pm.stop(proc.id)


async def test_read_log_chunks_do_not_split_utf8(pm, workspace):
    proc = await pm.start("printf 'aé€😀%.0s' $(seq 1 20)", cwd=workspace)
    await proc.watcher
    full, end = pm.read_log(proc.id)
    assert full == "aé€😀" * 20
    chunks, offset = [], 0
    for max_bytes in (1, 2, 3, 5) * 100:
        text, offset = pm.read_log(proc.id, offset=offset, max_bytes=max_bytes)
        chunks.append(text)
        if offset == end:
            break
    assert "".join(chunks) == full
    assert "�" not in "".join(chunks)


async def test_wait_for_pattern_and_port_timeout_message(pm, workspace):
    proc = await pm.start("sleep 3068", cwd=workspace)
    with pytest.raises(ToolError, match=r"waiting for 'ready' or port 1\b"):
        await pm.wait_for(proc.id, pattern="ready", port=1, timeout_s=0.3)
    await pm.stop(proc.id)


def test_port_open_sees_ipv6_only_listener():
    try:
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        server.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback not available")
    with server:
        server.listen()
        port = server.getsockname()[1]
        assert _port_open(port)
    assert not _port_open(port)


@needs_proc
@pytest.mark.parametrize("delay_s", [0.0, 0.001, 0.002, 0.004, 0.05])
async def test_stop_soon_after_start_leaves_nothing_running(tmp_path, workspace, rt, delay_s):
    # bwrap's in-sandbox init ignores SIGTERM and only later arms its parent-death signal, so a
    # stop a few ms after start used to orphan it, sometimes together with the command.
    pm = ProcessManager(rt.sandbox, rt.audit, tmp_path / "pm-state", max_processes=50)
    # Unique per run, so leftovers from an older run cannot fail this one.
    marker = f"sleep 3080.{secrets.randbelow(10**9)}"
    try:
        for _ in range(10):
            managed = await pm.start(marker, cwd=workspace)
            await asyncio.sleep(delay_s)
            await pm.stop(managed.id)
    finally:
        await pm.shutdown()
    await asyncio.sleep(0.2)
    alive = [e for e in Path("/proc").iterdir() if e.name.isdigit() and marker in _cmdline(e)]
    assert not alive


async def test_send_input_refused_while_earlier_input_is_unread(pm, workspace):
    proc = await pm.start("sleep 3111", cwd=workspace)
    with pytest.raises(ToolError, match="did not read its input"):
        await pm.send_input(proc.id, "x" * 2_000_000, timeout_s=0.3)
    # The unread rest sits in server memory; more input would only grow it.
    with pytest.raises(ToolError, match=r"has not read \d+ bytes of earlier input"):
        await pm.send_input(proc.id, "more", timeout_s=0.3)
    await pm.stop(proc.id)


async def test_concurrent_starts_respect_the_process_limit(tmp_path, workspace, rt):
    pm = ProcessManager(rt.sandbox, rt.audit, tmp_path / "pm-state", max_processes=2)
    try:
        results = await asyncio.gather(
            *(pm.start("sleep 3112", cwd=workspace) for _ in range(5)), return_exceptions=True
        )
        started = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, ToolError)]
        assert len(started) == 2 and len(refused) == 3, results
    finally:
        await pm.shutdown()


async def test_only_the_most_recent_exited_processes_are_kept(tmp_path, workspace, rt, monkeypatch):
    monkeypatch.setattr(ProcessManager, "keep_exited", 3)
    pm = ProcessManager(rt.sandbox, rt.audit, tmp_path / "pm-state", max_processes=8)
    try:
        server = await pm.start("sleep 3113", cwd=workspace)
        for _ in range(5):
            managed = await pm.start("true", cwd=workspace)
            assert managed.watcher is not None
            await managed.watcher
        ids = [p["id"] for p in pm.list()]
        # The running one is never dropped; of the exited ones only the newest three stay.
        assert ids == ["p1", "p4", "p5", "p6"]
        with pytest.raises(ToolError, match="unknown process id 'p2'"):
            pm.read_log("p2")
        assert server.running
    finally:
        await pm.shutdown()


async def test_stop_gives_up_on_a_process_that_does_not_die(tmp_path, workspace, monkeypatch):
    config = Config(workspace=workspace, state_dir=tmp_path / "state4").finalize()
    config.sandbox.backend = "none"
    audit = AuditStore(config.state_dir, secrets=[], result_bytes=1000)
    pm = ProcessManager(Sandbox(config), audit, config.state_dir, max_processes=4)
    monkeypatch.setattr("dev_link_mcp.processes.REAP_TIMEOUT_S", 0.3)
    try:
        proc = await pm.start("trap '' TERM; echo ready; while true; do sleep 0.1; done", cwd=workspace)
        await _wait_log(pm, proc.id, "ready")

        async def never() -> int:
            await asyncio.Event().wait()
            return 0

        # Stands in for a process stuck in uninterruptible sleep, which even SIGKILL cannot end.
        monkeypatch.setattr(proc.proc, "wait", never)
        started = time.monotonic()
        assert await pm.stop(proc.id, grace_s=0.2) is None
        assert time.monotonic() - started < 2
    finally:
        monkeypatch.undo()
        await pm.shutdown()
        audit.close()


async def test_wait_for_stops_waiting_on_a_catastrophic_pattern(pm, workspace):
    proc = await pm.start("echo " + "a" * 34 + "!; sleep 3114", cwd=workspace)
    await _wait_log(pm, proc.id, "!")
    started = time.monotonic()
    with pytest.raises(PatternStuck):
        await pm.wait_for(proc.id, pattern=r"^(a+)+$", timeout_s=0.5)
    assert time.monotonic() - started < 1.5
    assert "matched" in await pm.wait_for(proc.id, pattern=r"a!", timeout_s=10)
    await pm.stop(proc.id)


async def test_failed_registration_kills_the_started_process(pm, workspace, monkeypatch):
    marker = f"sleep {7400 + os.getpid() % 100}"

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(pm.audit, "start", broken)
    with pytest.raises(sqlite3.OperationalError):
        await pm.start(marker, cwd=workspace)
    assert pm.list() == []
    for _ in range(50):
        if subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode != 0:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"{marker} survived a failed start")


async def test_token_in_process_output_never_reaches_the_log_file(pm, rt, workspace):
    token = rt.config.server.token
    head, rest = token[:10], token[10:]
    # Whole, then split over two quick writes, then next to bytes that are not UTF-8.
    command = (
        f"printf 'whole-%s-end\\n' {token}; printf %s {head}; printf '%s-split\\n' {rest};"
        f" printf '\\377\\376%s\\n' {token}"
    )
    proc = await pm.start(command, cwd=workspace)
    async with asyncio.timeout(10):
        await proc.watcher
    raw = proc.log_path.read_bytes()
    assert token.encode() not in raw
    assert raw == b"whole-***-end\n***-split\n\xff\xfe***\n"
    text, offset = pm.read_log(proc.id)
    assert text.startswith("whole-***-end\n***-split\n") and offset == len(raw)
    assert token not in rt.audit.get(proc.event_id)["result"]


@needs_proc
async def test_stop_during_heavy_output_leaves_no_copy_behind(pm, workspace):
    proc = await pm.start("yes dev-link-flood", cwd=workspace)
    await _wait_log(pm, proc.id, "dev-link-flood\n")
    await pm.stop(proc.id)
    assert proc.pump._task is not None and proc.pump._task.done()
    open_files = {os.readlink(fd) for fd in Path("/proc/self/fd").iterdir() if fd.is_symlink()}
    assert str(proc.log_path) not in open_files
    size = proc.log_path.stat().st_size
    await asyncio.sleep(0.3)
    assert proc.log_path.stat().st_size == size


async def test_output_of_a_background_job_does_not_hold_up_exit_detection(tmp_path, workspace):
    # Unsandboxed, the job keeps the output pipe open after bash exits, until the exit cleanup
    # kills it; bwrap would end it together with bash.
    config = Config(workspace=workspace, state_dir=tmp_path / "state-bg").finalize()
    config.sandbox.backend = "none"
    audit = AuditStore(config.state_dir, secrets=[config.server.token], result_bytes=1000)
    pm = ProcessManager(Sandbox(config), audit, config.state_dir, max_processes=4)
    try:
        proc = await pm.start(f"sleep 3131 & echo started-{config.server.token}", cwd=workspace)
        async with asyncio.timeout(5):
            await proc.watcher
        assert proc.exit_code == 0 and pm.read_log(proc.id)[0] == "started-***\n"
    finally:
        await pm.shutdown()
        audit.close()


async def test_a_cancelled_stop_still_records_the_exit(tmp_path, workspace, monkeypatch):
    config = Config(workspace=workspace, state_dir=tmp_path / "state-cancel").finalize()
    config.sandbox.backend = "none"
    audit = AuditStore(config.state_dir, secrets=[config.server.token], result_bytes=1000)
    sandbox = Sandbox(config)
    # A job that escaped the exit cleanup keeps the output pipe open, so the watcher spends
    # PUMP_DRAIN_S draining it after bash exits.
    monkeypatch.setattr(sandbox, "reap", lambda pid: None)
    pm = ProcessManager(sandbox, audit, config.state_dir, max_processes=4)
    job = None
    try:
        proc = await pm.start("setsid sleep 3133 & echo job=$!; sleep 3134", cwd=workspace)
        job = int((await _wait_log(pm, proc.id, "\n")).strip().removeprefix("job="))
        stop = asyncio.create_task(pm.stop(proc.id, grace_s=1))
        async with asyncio.timeout(5):
            while proc.proc.returncode is None:
                await asyncio.sleep(0.02)
        stop.cancel()  # bash is gone and the watcher is still draining the pipe
        with contextlib.suppress(asyncio.CancelledError):
            await stop
        assert stop.cancelled()
        async with asyncio.timeout(5):
            await asyncio.shield(proc.watcher)
        assert proc.exit_code == -signal.SIGTERM
        assert audit.get(proc.event_id)["status"] == "ok"
        assert await pm.stop(proc.id) == -signal.SIGTERM
    finally:
        if job is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(job, signal.SIGKILL)
        await pm.shutdown()
        audit.close()


async def _pump(log_path: Path, secrets_: list[str]) -> tuple[LogPump, int]:
    """A started LogPump and a write end of its pipe that the test owns."""
    pump = LogPump(log_path, StreamRedactor(secrets_))
    write_fd = os.dup(pump.process_end.fileno())
    await pump.start()
    return pump, write_fd


async def test_held_output_reaches_the_log_once_the_process_pauses(tmp_path):
    token = secrets.token_urlsafe(24)
    log_path = tmp_path / "pump.log"
    pump, write_fd = await _pump(log_path, [token])
    try:
        # A token split over two quick writes is still caught.
        os.write(write_fd, f"a {token[:9]}".encode())
        await asyncio.sleep(0.02)
        os.write(write_fd, f"{token[9:]} b\n".encode())
        # A prompt that happens to end like the start of the token must not wait for more output.
        os.write(write_fd, f"Enter value: {token[:3]}".encode())
        expected = f"a *** b\nEnter value: {token[:3]}".encode()
        async with asyncio.timeout(0.6):
            while log_path.read_bytes() != expected:
                await asyncio.sleep(0.02)
    finally:
        os.close(write_fd)
        await pump.close(1)
    assert log_path.read_bytes() == expected


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full to fail log writes")
async def test_log_pump_keeps_draining_when_the_log_cannot_be_written(tmp_path, caplog):
    pump, write_fd = await _pump(Path("/dev/full"), [])
    data = b"x" * (1 << 20)  # far more than a pipe holds, so a pump that stopped reading would block the writer

    def write_all() -> None:
        with os.fdopen(write_fd, "wb") as out:
            out.write(data)

    try:
        with caplog.at_level(logging.WARNING, logger="dev_link_mcp.processes"):
            async with asyncio.timeout(10):
                await asyncio.to_thread(write_all)
            await pump.close(5)
    finally:
        await pump.close(0)
    warnings = [r for r in caplog.records if "cannot write process log" in r.getMessage()]
    assert len(warnings) == 1
