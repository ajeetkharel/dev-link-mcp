"""The dev-link command line: parsing, instance files, the tunnel, and real start/stop runs."""

from __future__ import annotations

import argparse
import fcntl
import importlib.metadata
import json
import logging
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import sandbox_flags

from dev_link_mcp.audit import AuditReader, AuditStore
from dev_link_mcp.cli import doctor, instance, main
from dev_link_mcp.cli import serve as serve_cmd
from dev_link_mcp.cli.main import parse_args, server_parser
from dev_link_mcp.cli.tunnel import Tunnel
from dev_link_mcp.config import load_config
from dev_link_mcp.config.schema import schema
from dev_link_mcp.private import open_private

REPO = Path(__file__).resolve().parents[1]

FAKE_CLOUDFLARED = """\
#!/bin/sh
echo "$$" > "{pidfile}"
echo "INF Requesting new quick Tunnel on trycloudflare.com..."
echo "INF |  https://fake-tunnel-test.trycloudflare.com  |"
exec sleep 300
"""

BROKEN_CLOUDFLARED = """\
#!/bin/sh
echo 'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: no network'
exit 1
"""


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _wait_gone(pid: int, timeout_s: float = 10) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    """Environment for dev-link subprocesses: private state root and config dir, no DEV_LINK_* settings."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEV_LINK_")}
    env["XDG_STATE_HOME"] = str(tmp_path / "state-root")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")
    return env


def run(env: dict[str, str], *args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dev_link_mcp", *args], env=env, capture_output=True, text=True, timeout=timeout
    )


@pytest.fixture
def busy_port() -> Iterator[int]:
    """A port with a listener on 127.0.0.1 for the length of the test."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock.getsockname()[1]


# --- parsing ---------------------------------------------------------------------------------


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    assert "COMMAND" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("dev-link ")


@pytest.mark.parametrize("command", ["serve", "start", "restart", "doctor", "tools"])
def test_server_flags_are_shared(command):
    args = parse_args([command, "--port", "9001", "--no-sandbox", "--enable", "docker", "--tunnel"])
    assert (args.port, args.no_sandbox, args.enable, args.tunnel) == (9001, True, ["docker"], True)
    assert args.workspace == "."


@pytest.mark.parametrize("command", ["stop", "status", "url", "dashboard"])
def test_instance_commands_take_dir_and_state_dir_only(command, capsys):
    args = parse_args([command, "/some/dir", "--state-dir", "/s"])
    assert (args.workspace, str(args.state_dir)) == ("/some/dir", "/s")
    with pytest.raises(SystemExit):
        parse_args([command, "--port", "1"])


def test_tunnel_and_public_url_conflict(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["serve", "--tunnel", "--public-url", "https://x.example"])
    assert exc.value.code == 2
    assert "--tunnel sets the public URL itself" in capsys.readouterr().err


def test_logs_all_cannot_follow(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["logs", "all", "-f"])
    assert exc.value.code == 2
    assert parse_args(["logs"]).target == "all"
    assert parse_args(["logs", "audit", "/w", "-f", "-n", "5"]).lines == 5


# --- ports and locks -----------------------------------------------------------------------


def test_an_unset_port_skips_a_busy_one(busy_port):
    assert not instance.port_free("127.0.0.1", busy_port)
    free = instance.first_free_port("127.0.0.1", busy_port, 20)
    assert free is not None and free > busy_port


def test_an_explicit_busy_port_is_refused(workspace, tmp_path, busy_port, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["start", str(workspace), "--state-dir", str(tmp_path / "s"), "--port", str(busy_port)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith(f"dev-link: port {busy_port} on 127.0.0.1 is in use (server.port set by cli)")


def test_a_second_serve_is_refused_while_the_lock_is_held(workspace, tmp_path, capsys):
    state = tmp_path / "s"
    state.mkdir()
    lock = instance.Lock.acquire(state)
    assert lock is not None
    try:
        assert instance.Lock.acquire(state, wait_s=0) is None
        with pytest.raises(SystemExit) as exc:
            main(["serve", str(workspace), "--state-dir", str(state)])
        assert exc.value.code == 1
        assert f"already running for {workspace}, PID {os.getpid()}; use dev-link stop" in capsys.readouterr().err
        assert main(["status", str(workspace), "--state-dir", str(state)]) == 0
    finally:
        lock.release()
    assert main(["status", str(workspace), "--state-dir", str(state)]) == 3
    assert capsys.readouterr().out.startswith("running   : PID")


def test_a_dead_holders_pid_is_not_running(workspace, tmp_path, capsys):
    state = tmp_path / "s"
    state.mkdir()
    holder = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from dev_link_mcp.cli.instance import Lock;"
            " Lock.acquire(Path(sys.argv[1]))",
            str(state),
        ],
        check=True,
    )
    assert holder.returncode == 0
    (state / instance.LOCK_FILE).write_text("1\n")  # a stale PID that must never be trusted
    assert instance.holder(state) is None
    assert main(["status", str(workspace), "--state-dir", str(state)]) == 3
    assert main(["stop", str(workspace), "--state-dir", str(state)]) == 0
    assert "is not running" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["url", str(workspace), "--state-dir", str(state)])
    assert exc.value.code == 1


def test_follow_reads_appends_and_restarts_after_truncation(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("old\n")
    lines = instance.follow(log, log.stat().st_size, poll_s=0.01)
    with log.open("a") as f:
        f.write("new line\n")
    assert next(lines) == "new line\n"
    log.write_text("after restart\n")
    assert next(lines) == "after restart\n"
    assert instance.tail(log, 5) == ["after restart"]


# --- start, status, url, logs, stop with real servers -------------------------------------


def test_start_status_url_logs_stop(tmp_path, cli_env):
    ws_a, ws_b = tmp_path / "ws-a", tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    started = run(cli_env, "start", str(ws_a), *sandbox_flags())
    assert started.returncode == 0, started.stderr
    assert f"dev-link started for {ws_a}" in started.stdout
    assert "dev-link stop" in started.stdout
    token = "cli-token-not-in-argv-0123"
    other = run(cli_env, "start", str(ws_b), "--token", token, *sandbox_flags())
    try:
        assert other.returncode == 0, other.stderr
        # Captured output is not a terminal, so the token stays out of it.
        assert token not in other.stdout
        assert "/mcp/***" in other.stdout and f"dev-link url {ws_b}" in other.stdout
        again = run(cli_env, "start", str(ws_a), *sandbox_flags())
        assert again.returncode == 1
        assert again.stderr.startswith(f"dev-link: already running for {ws_a}, PID ")

        status = run(cli_env, "status", str(ws_a))
        assert status.returncode == 0
        pid = int(status.stdout.split("PID ")[1].split()[0])

        url = run(cli_env, "url", str(ws_a))
        assert url.returncode == 0
        assert len(url.stdout.splitlines()) == 1
        state_a = Path(cli_env["XDG_STATE_HOME"]) / "dev-link-mcp"
        conn = json.loads(next(state_a.glob("ws-a-*/connection.json")).read_text())
        assert url.stdout.strip() == conn["mcp_url"] and urlsplit(conn["mcp_url"]).path.startswith("/mcp/")
        assert conn["pid"] == pid and conn["workspace"] == str(ws_a)
        conn_b = json.loads(next(state_a.glob("ws-b-*/connection.json")).read_text())
        assert urlsplit(conn["mcp_url"]).port != urlsplit(conn_b["mcp_url"]).port
        # A --token given to start reaches the server through its environment, not its argv.
        assert conn_b["mcp_url"].endswith(f"/mcp/{token}")
        args_b = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(conn_b["pid"])], capture_output=True, text=True)
        assert "dev_link_mcp serve" in args_b.stdout and token not in args_b.stdout

        everything = run(cli_env, "status", "--all")
        assert everything.returncode == 0
        assert str(ws_a) in everything.stdout and str(ws_b) in everything.stdout

        logs = run(cli_env, "logs", "server", str(ws_a), "-n", "50")
        assert logs.returncode == 0 and "Uvicorn running on" in logs.stdout
        # The server's output goes to server.log, not a terminal, so its banner hides the token.
        server_log_b = next(state_a.glob("ws-b-*/server.log")).read_text()
        assert token not in server_log_b and "/mcp/***" in server_log_b
        audit = run(cli_env, "logs", "audit", str(ws_a))
        assert audit.returncode == 0 and "server:start" in audit.stdout

        stopped = run(cli_env, "stop", str(ws_a))
        assert stopped.returncode == 0 and "stopped" in stopped.stdout
        assert _wait_gone(pid)
        after = run(cli_env, "status", str(ws_a))
        assert after.returncode == 3 and after.stdout.startswith("stopped")
        assert not list(state_a.glob("ws-a-*/connection.json"))
        # SIGTERM ran the server's own shutdown, which records the stop.
        reader = AuditReader(next(state_a.glob("ws-a-*")))
        try:
            assert reader.latest(1)[0]["name"] == "stop"
        finally:
            reader.close()
        assert run(cli_env, "url", str(ws_a)).returncode == 1
    finally:
        run(cli_env, "stop", str(ws_a))
        run(cli_env, "stop", str(ws_b))


# --- tunnel ----------------------------------------------------------------------------------


def _fake_cloudflared(tmp_path: Path, script: str) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    exe = bin_dir / "cloudflared"
    exe.write_text(script.format(pidfile=tmp_path / "cloudflared.pid"))
    exe.chmod(0o755)
    return bin_dir


def test_serve_with_tunnel_publishes_and_stops_it(tmp_path, workspace, cli_env):
    cli_env["PATH"] = f"{_fake_cloudflared(tmp_path, FAKE_CLOUDFLARED)}{os.pathsep}{cli_env['PATH']}"
    state = tmp_path / "s"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        [
            *(sys.executable, "-m", "dev_link_mcp", "serve", str(workspace), "--state-dir", str(state)),
            *("--port", str(port), "--tunnel", *sandbox_flags()),
        ],
        env=cli_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    tunnel_pid = None
    try:
        deadline = time.monotonic() + 60
        while not (state / "connection.json").exists():
            assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
            assert time.monotonic() < deadline
            time.sleep(0.1)
        tunnel_pid = int((tmp_path / "cloudflared.pid").read_text())
        conn = json.loads((state / "connection.json").read_text())
        assert conn["public_mcp_url"].startswith("https://fake-tunnel-test.trycloudflare.com/mcp/")
        assert stat.S_IMODE((state / "tunnel.log").stat().st_mode) == 0o600
        url = run(cli_env, "url", str(workspace), "--state-dir", str(state))
        assert url.stdout.strip() == conn["public_mcp_url"]
        # A foreground server is not a session leader, so stop signals only its PID; the server
        # has to stop the tunnel itself.
        stopped = run(cli_env, "stop", str(workspace), "--state-dir", str(state))
        assert stopped.returncode == 0, stopped.stderr
        assert proc.wait(15) == 0
        assert _wait_gone(tunnel_pid, 5)
        assert "anyone with the public URL can run commands" in (proc.stderr.read() if proc.stderr else "")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if tunnel_pid is not None and not _gone(tunnel_pid):
            os.kill(tunnel_pid, signal.SIGKILL)


def _serve_with_fake_tunnel(
    tmp_path: Path, workspace: Path, cli_env: dict[str, str]
) -> tuple[subprocess.Popen[str], int]:
    """A foreground `serve --tunnel` with the fake cloudflared, once it is ready: (server, cloudflared pid)."""
    cli_env["PATH"] = f"{_fake_cloudflared(tmp_path, FAKE_CLOUDFLARED)}{os.pathsep}{cli_env['PATH']}"
    state = tmp_path / "s"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        [
            *(sys.executable, "-m", "dev_link_mcp", "serve", str(workspace), "--state-dir", str(state)),
            *("--port", str(port), "--tunnel", *sandbox_flags()),
        ],
        env=cli_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 60
    while not (state / "connection.json").exists():
        if proc.poll() is not None or time.monotonic() >= deadline:
            proc.kill()
            raise AssertionError(proc.communicate()[1])
        time.sleep(0.1)
    return proc, int((tmp_path / "cloudflared.pid").read_text())


def test_sigkill_of_a_foreground_server_stops_its_tunnel(tmp_path, workspace, cli_env):
    proc, tunnel_pid = _serve_with_fake_tunnel(tmp_path, workspace, cli_env)
    try:
        proc.kill()
        proc.wait()
        assert _wait_gone(tunnel_pid, 5)
    finally:
        if not _gone(tunnel_pid):
            os.kill(tunnel_pid, signal.SIGKILL)


def test_a_tunnel_that_dies_mid_run_stops_the_server(tmp_path, workspace, cli_env):
    proc, tunnel_pid = _serve_with_fake_tunnel(tmp_path, workspace, cli_env)
    try:
        os.kill(tunnel_pid, signal.SIGKILL)
        assert proc.wait(15) == 1
        err = proc.stderr.read() if proc.stderr else ""
        assert "dev-link: cloudflared exited with code -9, so https://fake-tunnel-test" in err
        assert "trycloudflare.com no longer works; last lines of" in err
        assert "INF Requesting new quick Tunnel" in err
        assert not (tmp_path / "s" / "connection.json").exists()
        assert instance.holder(tmp_path / "s") is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_tunnel_that_exits_early_reports_its_log(tmp_path, workspace, monkeypatch, capsys):
    monkeypatch.setenv("PATH", f"{_fake_cloudflared(tmp_path, BROKEN_CLOUDFLARED)}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(SystemExit) as exc:
        main(["serve", str(workspace), "--state-dir", str(tmp_path / "s"), "--tunnel"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("dev-link: cloudflared exited with code 1 before it printed a tunnel URL")
    assert "ERR failed to request quick Tunnel" in err
    assert instance.holder(tmp_path / "s") is None


def test_a_missing_cloudflared_has_an_install_hint(tmp_path, workspace, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        main(["serve", str(workspace), "--state-dir", str(tmp_path / "s"), "--tunnel"])
    assert exc.value.code == 1
    assert "dev-link: cloudflared is not installed; install it" in capsys.readouterr().err


# --- doctor, config, tools -------------------------------------------------------------------


def test_doctor_fails_with_fixes_when_tools_are_missing(tmp_path, workspace, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(doctor, "_chromium", lambda: None)
    status = main(["doctor", str(workspace), "--state-dir", str(tmp_path / "s"), "--enable", "docker"])
    out = capsys.readouterr().out
    assert status == 1
    lines = out.splitlines()
    sandbox = next(i for i, line in enumerate(lines) if line.split()[1:2] == ["sandbox"])
    assert lines[sandbox].startswith("fail")
    assert "--no-sandbox" in lines[sandbox + 1] and "fix:" in lines[sandbox + 1]
    assert any(line.startswith("warn  rg") for line in lines)
    assert any(line.startswith("fail  docker") for line in lines)
    assert "fix: dev-link install-browser" in out
    assert not (tmp_path / "s" / "audit.sqlite3").exists()


def test_doctor_passes_with_only_warnings(tmp_path, workspace, monkeypatch, capsys):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name in ("rg", "cloudflared") else real(name))
    monkeypatch.setattr(doctor, "_chromium", lambda: "/fake/chrome")
    assert main(["doctor", str(workspace), "--state-dir", str(tmp_path / "s"), "--no-sandbox"]) == 0
    out = capsys.readouterr().out
    assert "warn  cloudflared" in out and "fix: install cloudflared" in out


def test_doctor_fails_when_the_sandbox_would_mount_the_state_dir(tmp_path, workspace, capsys):
    state = tmp_path / "s"
    conf = tmp_path / "c.yaml"
    conf.write_text(f"sandbox:\n  rw_paths: [{state / 'shared'}]\n")
    assert main(["doctor", str(workspace), "--state-dir", str(state), "-c", str(conf)]) == 1
    lines = capsys.readouterr().out.splitlines()
    sandbox = next(i for i, line in enumerate(lines) if line.split()[1:2] == ["sandbox"])
    assert lines[sandbox].startswith("fail") and "sandbox.rw_paths entry" in lines[sandbox]
    assert "move state_dir" in lines[sandbox + 1]


def test_doctor_fails_when_a_mount_source_is_inside_a_writable_mount(tmp_path, workspace, capsys):
    conf = tmp_path / "c.yaml"
    conf.write_text(f"sandbox:\n  rw_paths: [{workspace / 'out'}]\n")
    assert main(["doctor", str(workspace), "--state-dir", str(tmp_path / "s"), "-c", str(conf)]) == 1
    lines = capsys.readouterr().out.splitlines()
    sandbox = next(i for i, line in enumerate(lines) if line.split()[1:2] == ["sandbox"])
    assert lines[sandbox].startswith("fail") and "inside the writable mount workspace" in lines[sandbox]
    assert "fix:" in lines[sandbox + 1] and "out of" in lines[sandbox + 1]


def test_doctor_reports_a_bad_config(tmp_path, workspace, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("server:\n  prot: 1\n")
    assert main(["doctor", str(workspace), "--state-dir", str(tmp_path / "s"), "-c", str(bad)]) == 1
    assert f"fail  config       unknown config key: server.prot [from config file {bad}]" in capsys.readouterr().out


def test_config_show_labels_sources_and_hides_the_token(tmp_path, workspace, monkeypatch, capsys):
    user_file = tmp_path / "xdg-config" / "dev-link" / "config.yaml"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("server:\n  host: localhost\n")
    monkeypatch.setenv("DEV_LINK_LOG_LEVEL", "debug")
    token = "secret-token-0123456789"
    main(["config", "show", str(workspace), "--state-dir", str(tmp_path / "s"), "--token", token, "--port", "9100"])
    out = capsys.readouterr().out
    assert token not in out
    assert "  token: '***'  # cli" in out
    assert "  port: 9100  # cli" in out
    assert "log_level: DEBUG  # env DEV_LINK_LOG_LEVEL" in out
    assert f"  host: localhost  # user file {user_file}" in out
    assert "  network: true  # default" in out


def test_config_init_refuses_to_overwrite(tmp_path, workspace, capsys):
    path = tmp_path / "xdg-config" / "dev-link" / "config.yaml"
    assert main(["config", "init"]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cfg = load_config(None, {"workspace": str(workspace), "state_dir": tmp_path / "s"}, environ={}, user_file=path)
    assert cfg.sources["server.port"] == "default"
    path.write_text("server:\n  port: 9200\n")
    with pytest.raises(SystemExit) as exc:
        main(["config", "init"])
    assert exc.value.code == 1
    assert "already exists; pass --force" in capsys.readouterr().err
    assert path.read_text() == "server:\n  port: 9200\n"
    assert main(["config", "init", "--force"]) == 0
    assert "port: 9200" not in path.read_text()
    assert main(["config", "path"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"{path} (exists)"


def test_config_schema_matches_the_docs(capsys):
    assert main(["config", "schema"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == schema() == json.loads((REPO / "docs" / "config.schema.json").read_text())


def test_tools_lists_groups_without_touching_the_audit_store(tmp_path, workspace, capsys):
    state = tmp_path / "s"
    state.mkdir()
    store = AuditStore(state, secrets=[], result_bytes=1000)
    store.record("server", "start")
    store.close()
    assert main(["tools", str(workspace), "--state-dir", str(state), "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert "read_file" in {t["name"] for t in listing["groups"]["files"]}
    assert all(t["description"] for tools in listing["groups"].values() for t in tools)
    assert "docker" in listing["disabled"]
    reader = AuditReader(state)
    try:
        assert len(reader.latest(10)) == 1
    finally:
        reader.close()
    assert main(["tools", str(workspace), "--state-dir", str(state), "--group", "http"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("http (") and "\nfiles (" not in out
    with pytest.raises(SystemExit):
        main(["tools", str(workspace), "--state-dir", str(state), "--group", "nope"])


def test_install_browser_without_playwright(monkeypatch, capsys):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "playwright" else real(name, *a))
    with pytest.raises(SystemExit) as exc:
        main(["install-browser"])
    assert exc.value.code == 1
    assert "pip install 'dev-link-mcp[browser]'" in capsys.readouterr().err


# --- start, locks, signals, logs, imports, flags ---------------------------------------------


def test_healthy_treats_a_garbage_answer_as_not_healthy():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]

        def answer() -> None:
            conn, _ = server.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(b"HELLO\r\n\r\n")

        thread = threading.Thread(target=answer)
        thread.start()
        try:
            assert serve_cmd._healthy(f"http://127.0.0.1:{port}/health") is False
        finally:
            thread.join(5)


def test_start_fails_when_the_child_exits_even_if_something_answers(tmp_path, workspace, monkeypatch, capsys):
    # Another server on the port could answer /health; only the child's own connection.json counts.
    monkeypatch.setattr(serve_cmd, "_healthy", lambda url: True)
    with pytest.raises(SystemExit) as exc:
        main(["start", str(workspace), "--state-dir", str(tmp_path / "s"), "--enable", "dokcer", *sandbox_flags()])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("dev-link: the server exited with code 2")
    assert "unknown tool group" in err


def _hold(path: Path, content: str) -> int:
    """An fd holding the lock on `path` without a PID written by Lock.acquire."""
    path.write_text(content)
    fd = os.open(path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


@pytest.mark.parametrize("content", ["", "1999999999\n"])
def test_holder_sees_a_release_while_it_waits_for_the_pid(tmp_path, content):
    # A lock held on an empty file, or with a dead holder's PID still in it, is released before
    # any live PID appears: nothing runs.
    fd = _hold(tmp_path / instance.LOCK_FILE, content)
    timer = threading.Timer(0.3, os.close, (fd,))
    timer.start()
    try:
        assert instance.holder(tmp_path) is None
    finally:
        timer.join()


def test_holder_waits_for_the_new_holders_pid(tmp_path):
    # Between flock and writing its PID, a new server's lock file still holds a dead server's PID.
    path = tmp_path / instance.LOCK_FILE
    fd = _hold(path, "1999999999\n")

    def write_pid() -> None:
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)

    timer = threading.Timer(0.3, write_pid)
    timer.start()
    try:
        assert instance.holder(tmp_path) == os.getpid()
    finally:
        timer.join()
        os.close(fd)


def test_serve_asks_to_retry_when_the_holder_just_left(tmp_path, workspace, monkeypatch, capsys):
    monkeypatch.setattr(instance.Lock, "acquire", classmethod(lambda cls, state_dir, wait_s=0.5: None))
    monkeypatch.setattr(instance, "holder", lambda state_dir: None)
    with pytest.raises(SystemExit) as exc:
        main(["serve", str(workspace), "--state-dir", str(tmp_path / "s")])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "PID None" not in err and "try again" in err


def test_a_second_sigterm_does_not_interrupt_the_shutdown():
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit):
            serve_cmd._terminate(signal.SIGTERM, None)
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_follow_keeps_multibyte_characters_whole(tmp_path):
    log = tmp_path / "server.log"
    log.write_bytes(b"")
    chunks = instance.follow(log, 0, poll_s=0.01)
    snowman = "☃".encode()
    with log.open("ab") as f:
        f.write(b"a" + snowman[:1])
    first = next(chunks)
    with log.open("ab") as f:
        f.write(snowman[1:] + b"\n")
    assert first + next(chunks) == "a☃\n"


def test_logs_audit_follow_with_no_backlog_prints_only_new_events(tmp_path, workspace, cli_env):
    state = tmp_path / "s"
    state.mkdir()
    store = AuditStore(state, secrets=[], result_bytes=1000)
    for i in range(3):
        store.record("tool", f"old_{i}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "dev_link_mcp", "logs", "audit", str(workspace), "--state-dir", str(state), "-fn0"],
        env=cli_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1.5)
        store.record("tool", "new_one")
        time.sleep(2)
    finally:
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
        store.close()
    assert "old_" not in out, out
    assert "new_one" in out, (out, err)


def test_quick_commands_do_not_load_the_server_stack():
    probe = (
        "import sys, dev_link_mcp.cli.serve, dev_link_mcp.cli.info, dev_link_mcp.cli.config_cmd;"
        " print(sorted(m for m in sys.modules if m.split('.')[0] in ('uvicorn', 'mcp', 'starlette')))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_open_private_narrows_an_existing_file(tmp_path):
    path = tmp_path / "connection.json"
    path.write_text("old")
    path.chmod(0o644)
    fd = open_private(path)
    try:
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        assert path.read_text() == ""
    finally:
        os.close(fd)


def _every_server_flag(skip: str) -> list[str]:
    """argv that sets every server option except `skip`, with a value of the right shape."""
    argv: list[str] = []
    for action in server_parser()._actions:
        flag = action.option_strings[-1] if action.option_strings else None
        if flag is None or flag in ("-h", "--help", skip):
            continue
        if action.nargs == 0:
            argv.append(flag)
        elif action.choices:
            argv += [flag, list(action.choices)[-1]]
        elif action.type is int:
            argv += [flag, "9001"]
        else:
            argv += [flag, f"value-{action.dest}"]
            if isinstance(action, argparse._AppendAction):
                argv += [flag, f"second-{action.dest}"]
    return argv


@pytest.mark.parametrize("skip", ["--tunnel", "--public-url"])
def test_start_forwards_every_server_flag(skip):
    start = parse_args(["start", "/w", "-c", "/c.yaml", *_every_server_flag(skip)])
    served = parse_args(["serve", "/w", *serve_cmd._server_flags(start)])
    # start passes the port and state dir itself, and the token through the environment.
    own = {"port", "token", "state_dir", "workspace", "command"}
    expected = {k: v for k, v in vars(start).items() if k not in own}
    assert {k: v for k, v in vars(served).items() if k not in own} == expected
    assert "--token" not in serve_cmd._server_flags(start)


def test_doctor_names_the_extra_for_a_missing_group(tmp_path, workspace, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "missing_requirements", lambda b: list(b.requires))
    assert main(["doctor", str(workspace), "--state-dir", str(tmp_path / "s"), "--no-sandbox"]) in (0, 1)
    out = capsys.readouterr().out
    assert "warn  browser      playwright not installed" in out
    assert "fix: pip install 'dev-link-mcp[browser]'" in out
    assert "fix: pip install 'dev-link-mcp[code]'" in out


def test_server_logging_redacts_the_token(config, monkeypatch, capsys):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    serve_cmd.configure_logging(config)
    token = config.server.token
    try:
        raise RuntimeError(f"no route for /ui/{token}/")
    except RuntimeError:
        logging.getLogger("dev_link_mcp.test").exception("request to /mcp/%s failed", token)
    err = capsys.readouterr().err
    assert token not in err
    assert "request to /mcp/*** failed" in err and "RuntimeError: no route for /ui/***/" in err


def test_server_logging_redacts_arguments_that_do_not_fit_the_format(config, monkeypatch, capsys):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    serve_cmd.configure_logging(config)
    token = config.server.token
    # Formatting fails here, and logging's error report would print the raw arguments to stderr.
    logging.getLogger("dev_link_mcp.test").warning("bad format %d", f"/mcp/{token}")
    err = capsys.readouterr().err
    assert token not in err
    assert "bad format %d ('/mcp/***',)" in err and "Logging error" not in err


def test_tunnel_log_hides_the_token(tmp_path, monkeypatch):
    token = "tunnel-token-0123456789abcdef"
    # cloudflared logs the full URL of a request it could not pass on, path token included.
    script = FAKE_CLOUDFLARED.replace(
        "exec sleep 300",
        f'echo "ERR Request failed error=\\"connection refused\\" dest=https://fake-tunnel-test.trycloudflare.com/mcp/{token}"\n'
        "exec sleep 300",
    )
    monkeypatch.setenv("PATH", f"{_fake_cloudflared(tmp_path, script)}{os.pathsep}{os.environ['PATH']}")
    log_path = tmp_path / "tunnel.log"
    tunnel = Tunnel.start("http://127.0.0.1:1", log_path, secrets=[token])
    try:
        assert tunnel.url == "https://fake-tunnel-test.trycloudflare.com"
    finally:
        tunnel.stop()
    text = log_path.read_text()
    assert token not in text and "trycloudflare.com/mcp/***" in text
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full to fail log writes")
def test_tunnel_log_copy_keeps_reading_after_a_write_failure():
    from dev_link_mcp.audit import StreamRedactor
    from dev_link_mcp.cli.tunnel import _copy

    read_fd, write_fd = os.pipe()
    source = open(read_fd, "rb", buffering=0)  # noqa: SIM115 - _copy closes it
    log_file = open("/dev/full", "wb", buffering=0)  # noqa: SIM115 - _copy closes it
    copier = threading.Thread(target=_copy, args=(source, log_file, StreamRedactor([])), daemon=True)
    copier.start()
    written: list[int] = []

    def write() -> None:
        # Far more than a pipe holds, so it only all goes through if the copy keeps reading.
        with open(write_fd, "wb", buffering=0) as out:
            written.append(out.write(b"x" * (1 << 20)))

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(10)
    copier.join(10)
    assert not writer.is_alive() and not copier.is_alive()
    assert written == [1 << 20]
    assert source.closed and log_file.closed


def test_cli_is_installed_under_both_names():
    # `uvx dev-link-mcp` runs the command named like the package, so both names must exist.
    scripts = {ep.name: ep.value for ep in importlib.metadata.distribution("dev-link-mcp").entry_points}
    assert scripts["dev-link"] == scripts["dev-link-mcp"] == "dev_link_mcp.cli:main"
