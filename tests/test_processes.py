import re
import socket

import pytest
from conftest import text


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _field(out: str, key: str) -> str:
    match = re.search(rf"\b{key}=(\S+)", out)
    assert match, f"{key} missing in:\n{out}"
    return match.group(1)


async def test_tools_are_registered_with_expected_annotations(connect):
    async with connect("processes") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "start_process",
        "list_processes",
        "process_output",
        "process_input",
        "stop_process",
        "wait_for_process",
    }
    assert tools["list_processes"].annotations.read_only_hint
    assert tools["process_output"].annotations.read_only_hint
    assert tools["start_process"].annotations.destructive_hint
    assert tools["stop_process"].annotations.destructive_hint
    assert "ctx" not in tools["start_process"].input_schema["properties"]
    # Network is shared by default, so background commands are open-world like run_command.
    assert tools["start_process"].annotations.open_world_hint


async def test_start_process_returns_id_and_initial_output(connect, rt):
    async with connect("processes") as client:
        r = await client.call_tool("start_process", {"command": "echo hello-dev-link; sleep 30", "name": "greeter"})
        assert not r.is_error, text(r)
        out = text(r)
        assert "id=p1" in out
        assert "name=greeter" in out
        assert "status=running" in out
        assert "hello-dev-link" in out
        assert int(_field(out, "next_offset")) > 0
    assert rt.audit.query(kind="process")[0]["name"] == "greeter"


async def test_start_process_waits_for_pattern(connect):
    async with connect("processes") as client:
        r = await client.call_tool(
            "start_process",
            {"command": "sleep 0.7; echo 'server ready on 3000'; sleep 30", "wait_for_pattern": r"ready on \d+"},
        )
        out = text(r)
    assert not r.is_error, out
    assert "log matched" in out
    assert "server ready on 3000" in out


async def test_start_process_waits_for_port(connect):
    port = _free_port()
    command = f"python3 -m http.server {port} --bind 127.0.0.1"
    async with connect("processes") as client:
        r = await client.call_tool("start_process", {"command": command, "wait_for_port": port, "wait_timeout_s": 20})
        out = text(r)
        assert not r.is_error, out
        assert f"port {port} is accepting connections" in out
        stop = await client.call_tool("stop_process", {"id": _field(out, "id")})
    assert not stop.is_error


async def test_start_process_wait_timeout_is_reported_and_process_keeps_running(connect, rt):
    async with connect("processes") as client:
        r = await client.call_tool(
            "start_process", {"command": "sleep 30", "wait_for_pattern": "never", "wait_timeout_s": 0.5}
        )
        out = text(r)
    assert not r.is_error, out
    assert "timed out" in out
    assert "still running" in out
    assert rt.processes.get("p1").running


async def test_start_process_reports_immediate_exit(connect):
    async with connect("processes") as client:
        r = await client.call_tool("start_process", {"command": "echo boom; exit 3", "wait_for_pattern": "never"})
    out = text(r)
    assert "process exited with code 3" in out
    assert "boom" in out


async def test_start_process_env_and_cwd(connect, workspace):
    (workspace / "sub").mkdir()
    async with connect("processes") as client:
        r = await client.call_tool(
            "start_process",
            {
                "command": 'echo "$GREETING from $(pwd)"',
                "cwd": "sub",
                "env": {"GREETING": "hi"},
                "wait_for_pattern": "from",
            },
        )
        out = text(r)
    assert f"hi from {workspace / 'sub'}" in out
    assert "cwd=sub" in out


async def test_start_process_validation_errors(connect, workspace):
    (workspace / "file.txt").write_text("x")
    cases = [
        ({"command": "   "}, "command is empty"),
        ({"command": "true", "wait_for_pattern": "("}, "invalid regex"),
        ({"command": "true", "wait_for_port": 70000}, "port must be between"),
        ({"command": "true", "wait_for_port": 0}, "port must be between"),
        ({"command": "true", "cwd": "missing"}, "does not exist"),
        ({"command": "true", "cwd": "file.txt"}, "not a directory"),
        ({"command": "echo a\0b"}, "NUL byte"),
        ({"command": "true", "env": {"A=B": "x"}}, "invalid environment variable name"),
        ({"command": "true", "env": {"": "x"}}, "invalid environment variable name"),
        ({"command": "true", "env": {"A": "x\0y"}}, "NUL byte"),
        ({"command": "true", "name": "a\nid=p99"}, "printable"),
        ({"command": "true", "name": "x" * 81}, "printable"),
    ]
    async with connect("processes") as client:
        for args, message in cases:
            r = await client.call_tool("start_process", args)
            assert r.is_error, args
            assert message in text(r), (args, text(r))
        r = await client.call_tool("start_process", {"command": "true", "wait_timeout_s": 0})
        assert r.is_error


@pytest.mark.bwrap
async def test_start_process_rejects_cwd_outside_workspace(connect, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside)
    async with connect("processes") as client:
        for cwd in ("../", str(outside), "link", "/"):
            r = await client.call_tool("start_process", {"command": "true", "cwd": cwd})
            assert r.is_error, cwd
            assert "outside the workspace" in text(r)


async def test_process_limit(connect, rt):
    rt.processes.max_processes = 1
    async with connect("processes") as client:
        first = await client.call_tool("start_process", {"command": "sleep 30"})
        assert not first.is_error
        second = await client.call_tool("start_process", {"command": "sleep 30"})
    assert second.is_error
    assert "process limit" in text(second)


async def test_list_processes_structured(connect, workspace):
    async with connect("processes") as client:
        empty = await client.call_tool("list_processes", {})
        assert empty.structured_content == {"processes": []}
        await client.call_tool("start_process", {"command": "sleep 30", "name": "sleeper"})
        await client.call_tool("start_process", {"command": "exit 0", "wait_for_pattern": "never"})
        r = await client.call_tool("list_processes", {})
    procs = r.structured_content["processes"]
    assert [p["id"] for p in procs] == ["p1", "p2"]
    assert procs[0]["name"] == "sleeper"
    assert procs[0]["status"] == "running"
    assert procs[0]["cwd"] == "."
    assert procs[1]["status"] == "exited(0)"


async def test_process_output_offsets_and_tail(connect):
    async with connect("processes") as client:
        r = await client.call_tool(
            "start_process",
            {"command": "for i in 1 2 3 4 5; do echo line$i; done; echo done", "wait_for_pattern": "done"},
        )
        pid = _field(text(r), "id")
        full = await client.call_tool("process_output", {"id": pid})
        out = text(full)
        assert not full.is_error
        assert "line1" in out and "line5" in out
        end = int(_field(out, "next_offset"))

        again = await client.call_tool("process_output", {"id": pid, "offset": end})
        assert "(no new output)" in text(again)
        assert int(_field(text(again), "next_offset")) == end

        partial = await client.call_tool("process_output", {"id": pid, "offset": 6})
        assert "line1" not in text(partial)
        assert "line2" in text(partial)

        tail = await client.call_tool("process_output", {"id": pid, "tail_lines": 2})
        tail_body = text(tail).split("--- output ---\n", 1)[1]
        assert tail_body.splitlines() == ["line5", "done"]

        beyond = await client.call_tool("process_output", {"id": pid, "offset": 10_000_000})
        assert int(_field(text(beyond), "next_offset")) == end


async def test_process_output_reports_more_when_capped(connect, rt):
    rt.config.limits.max_output_bytes = 1000
    async with connect("processes") as client:
        r = await client.call_tool(
            "start_process",
            {"command": "head -c 5000 /dev/zero | tr '\\0' x; echo; echo END", "wait_for_pattern": "END"},
        )
        pid = _field(text(r), "id")
        assert "more output available" in text(r)
        first = await client.call_tool("process_output", {"id": pid})
        assert "more output available" in text(first)
        assert int(_field(text(first), "next_offset")) == 1000
        offset, chunks = 0, ""
        for _ in range(10):
            page = await client.call_tool("process_output", {"id": pid, "offset": offset})
            chunks += text(page).split("--- output ---\n", 1)[1]
            offset = int(_field(text(page), "next_offset"))
            if "more output available" not in text(page):
                break
    assert "END" in chunks


async def test_process_output_validation(connect):
    async with connect("processes") as client:
        r = await client.call_tool("process_output", {"id": "nope"})
        assert r.is_error
        assert "unknown process id" in text(r)
        await client.call_tool("start_process", {"command": "sleep 30"})
        assert (await client.call_tool("process_output", {"id": "p1", "offset": -1})).is_error
        assert (await client.call_tool("process_output", {"id": "p1", "tail_lines": 0})).is_error


async def test_process_input_drives_stdin(connect):
    script = 'while read -r line; do echo "got:$line"; done'
    async with connect("processes") as client:
        await client.call_tool("start_process", {"command": script})
        r = await client.call_tool("process_input", {"id": "p1", "text": "alpha"})
        assert not r.is_error, text(r)
        assert "sent 6 bytes" in text(r)
        w = await client.call_tool("wait_for_process", {"id": "p1", "pattern": "got:alpha", "timeout_s": 10})
        assert not w.is_error, text(w)

        await client.call_tool("process_input", {"id": "p1", "text": "be", "newline": False})
        await client.call_tool("process_input", {"id": "p1", "text": "ta"})
        w = await client.call_tool("wait_for_process", {"id": "p1", "pattern": "got:beta", "timeout_s": 10})
        assert not w.is_error, text(w)


async def test_process_input_errors(connect):
    async with connect("processes") as client:
        r = await client.call_tool("process_input", {"id": "p9", "text": "x"})
        assert r.is_error and "unknown process id" in text(r)
        await client.call_tool("start_process", {"command": "exit 0", "wait_for_pattern": "never"})
        r = await client.call_tool("process_input", {"id": "p1", "text": "x"})
    assert r.is_error
    assert "not running" in text(r)


async def test_stop_process(connect, rt):
    async with connect("processes") as client:
        await client.call_tool("start_process", {"command": "echo started; sleep 60", "wait_for_pattern": "started"})
        r = await client.call_tool("stop_process", {"id": "p1"})
        out = text(r)
        assert not r.is_error, out
        assert "status=exited" in out
        assert "started" in out
        assert not rt.processes.get("p1").running
        again = await client.call_tool("stop_process", {"id": "p1"})
        assert not again.is_error
        missing = await client.call_tool("stop_process", {"id": "p42"})
    assert missing.is_error
    events = rt.audit.query(kind="process")
    assert events[0]["status"] == "ok"


async def test_stop_process_kills_children(connect, rt):
    async with connect("processes") as client:
        await client.call_tool("start_process", {"command": "sleep 60 & sleep 60 & wait; echo after"})
        await client.call_tool("stop_process", {"id": "p1"})
        r = await client.call_tool("process_output", {"id": "p1"})
    assert "after" not in text(r)


async def test_wait_for_process(connect):
    async with connect("processes") as client:
        await client.call_tool("start_process", {"command": "sleep 0.5; echo ready; sleep 0.3; exit 7"})
        r = await client.call_tool("wait_for_process", {"id": "p1", "pattern": "ready", "timeout_s": 10})
        assert not r.is_error, text(r)
        assert "log matched" in text(r)

        r = await client.call_tool("wait_for_process", {"id": "p1", "timeout_s": 10})
        assert not r.is_error, text(r)
        assert "process exited with code 7" in text(r)


async def test_wait_for_process_timeout_and_validation(connect, rt):
    async with connect("processes") as client:
        await client.call_tool("start_process", {"command": "sleep 30"})
        r = await client.call_tool("wait_for_process", {"id": "p1", "pattern": "never", "timeout_s": 0.5})
        assert r.is_error
        assert "timed out" in text(r)
        assert rt.processes.get("p1").running

        bad = [
            ({"id": "p1", "pattern": "[", "timeout_s": 1}, "invalid regex"),
            ({"id": "p1", "port": 99999, "timeout_s": 1}, "port must be between"),
            ({"id": "zz", "timeout_s": 1}, "unknown process id"),
        ]
        for args, message in bad:
            r = await client.call_tool("wait_for_process", args)
            assert r.is_error, args
            assert message in text(r)
        assert (await client.call_tool("wait_for_process", {"id": "p1", "timeout_s": -1})).is_error


async def test_wait_for_process_port(connect):
    port = _free_port()
    async with connect("processes") as client:
        await client.call_tool(
            "start_process", {"command": f"sleep 0.5; python3 -m http.server {port} --bind 127.0.0.1"}
        )
        r = await client.call_tool("wait_for_process", {"id": "p1", "port": port, "timeout_s": 20})
    assert not r.is_error, text(r)
    assert f"port {port} is accepting connections" in text(r)


async def test_process_output_paging_keeps_utf8_characters_whole(connect, rt):
    rt.config.limits.max_output_bytes = 1001
    async with connect("processes") as client:
        r = await client.call_tool(
            "start_process",
            {"command": "for i in $(seq 1 1500); do printf 'é'; done; echo; echo END", "wait_for_pattern": "END"},
        )
        pid = _field(text(r), "id")
        await client.call_tool("wait_for_process", {"id": pid, "timeout_s": 10})
        offset, chunks = 0, ""
        for _ in range(10):
            page = await client.call_tool("process_output", {"id": pid, "offset": offset})
            chunks += text(page).split("--- output ---\n", 1)[1]
            offset = int(_field(text(page), "next_offset"))
            if "more output available" not in text(page):
                break
    assert "\ufffd" not in chunks
    assert chunks.count("é") == 1500
    assert "END" in chunks


async def test_wait_for_process_timeout_names_the_target(connect):
    async with connect("processes") as client:
        await client.call_tool("start_process", {"command": "sleep 30"})
        r = await client.call_tool("wait_for_process", {"id": "p1", "timeout_s": 0.3})
        assert r.is_error
        assert "waiting for the process to exit" in text(r)
        r = await client.call_tool("wait_for_process", {"id": "p1", "port": 1, "pattern": "x", "timeout_s": 0.3})
        assert "pattern 'x' or port 1" in text(r)
        r = await client.call_tool("start_process", {"command": "sleep 30", "wait_for_port": 1, "wait_timeout_s": 0.3})
    assert not r.is_error
    assert "waiting for port 1; the process is still running" in text(r)
