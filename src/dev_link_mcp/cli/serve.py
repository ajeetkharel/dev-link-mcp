"""Running an instance: serve, start, stop, restart and status."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from types import FrameType
from urllib.parse import urlsplit

from dev_link_mcp.audit import redact
from dev_link_mcp.cli import instance
from dev_link_mcp.cli.common import CliError, resolve, where_args
from dev_link_mcp.cli.tunnel import Tunnel
from dev_link_mcp.config import Config, default_state_root
from dev_link_mcp.private import open_private

STATUS_STOPPED = 3


def _terminate(sig: int, frame: FrameType | None) -> None:
    # uvicorn handles SIGTERM while it runs and raises it again when it is done; turning it into
    # SystemExit lets the shutdown in serve() run instead of the default handler killing the process.
    # A second SIGTERM would abort that shutdown halfway, so later ones are ignored; `dev-link stop`
    # still sends SIGKILL if the shutdown takes too long.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(0)


def cmd_serve(args: argparse.Namespace) -> int:
    # The server stack (uvicorn, the MCP SDK, every tool group) loads only here, so stop and
    # status stay quick.
    from dev_link_mcp.app import ServeAborted, serve
    from dev_link_mcp.sandbox import SandboxUnavailable
    from dev_link_mcp.tools import ToolGroupError

    config = resolve(args)
    assert config.state_dir is not None
    if args.tunnel:
        if config.server.public_url:
            source = config.sources.get("server.public_url", "config")
            raise CliError(f"--tunnel sets the public URL itself; remove server.public_url (set by {source})", code=2)
        if config.server.auth == "none":
            raise CliError("--tunnel needs auth path-token or bearer; auth 'none' would publish the server", code=2)
    lock = instance.Lock.acquire(config.state_dir)
    if lock is None:
        pid = instance.holder(config.state_dir)
        if pid is not None:
            raise CliError(f"already running for {config.workspace}, PID {pid}; use dev-link stop")
        # The holder exited between the two checks.
        lock = instance.Lock.acquire(config.state_dir)
        if lock is None:
            raise CliError(f"another server for {config.workspace} just started or stopped; try again")
    tunnel: Tunnel | None = None
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        if args.tunnel:
            tunnel = Tunnel.start(
                config.base_url(), config.state_dir / instance.TUNNEL_LOG, secrets=[config.server.token]
            )
            config.server.public_url = tunnel.url
        configure_logging(config)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(
                serve(
                    config,
                    on_shutdown=tunnel.stop if tunnel else None,
                    # A server whose public URL is dead would look healthy to `dev-link status`
                    # while remote clients fail, so it stops and says why.
                    abort_when=tunnel.failure if tunnel else None,
                )
            )
    except (ToolGroupError, SandboxUnavailable) as exc:
        raise CliError.from_exc(exc) from None
    except ServeAborted as exc:
        log_path = config.state_dir / instance.TUNNEL_LOG
        raise CliError(f"{exc}; last lines of {log_path}:", details=instance.tail(log_path, 20)) from None
    finally:
        if tunnel is not None:
            tunnel.stop()
        signal.signal(signal.SIGTERM, previous)
        lock.release()
    return 0


def configure_logging(config: Config) -> None:
    """Send log records to stderr at `config.log_level`, with the server token replaced by ***
    in each one, tracebacks included. Like logging.basicConfig, does nothing when the root
    logger already has handlers."""
    from dev_link_mcp.audit import SecretLogFilter

    # On the handler, not the root logger: records from other loggers reach root's handlers
    # without passing root's own filters.
    handler = logging.StreamHandler()
    handler.addFilter(SecretLogFilter([config.server.token]))
    logging.basicConfig(
        level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[handler]
    )


def _server_flags(args: argparse.Namespace) -> list[str]:
    """The server flags the user gave, as argv for `serve`. Leaves out DIR, --state-dir and --port,
    which start passes itself, and --token, which goes through the environment so `ps` does not
    show it."""
    out: list[str] = []
    for flag, value in (
        ("--config", args.config),
        ("--host", args.host),
        ("--auth", args.auth),
        ("--log-level", args.log_level),
        ("--public-url", args.public_url),
    ):
        if value is not None:
            out += [flag, str(value)]
    for flag, values in (("--enable", args.enable), ("--disable", args.disable), ("--allowed-host", args.allowed_host)):
        for value in values:
            out += [flag, value]
    for flag, on in (("--no-sandbox", args.no_sandbox), ("--no-network", args.no_network), ("--tunnel", args.tunnel)):
        if on:
            out.append(flag)
    return out


def _choose_port(config: Config) -> int:
    host, port = config.server.host, config.server.port
    if config.sources.get("server.port", "default") != "default":
        if not instance.port_free(host, port):
            source = config.sources["server.port"]
            raise CliError(f"port {port} on {host} is in use (server.port set by {source}); choose another with --port")
        return port
    free = instance.first_free_port(host, port)
    if free is None:
        raise CliError(f"no free port between {port} and {port + instance.PORT_SEARCH - 1} on {host}; pass --port")
    return free


def _healthy(url: str) -> bool:
    # No proxy: a configured HTTP proxy cannot reach this machine's loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1) as response:
            return bool(response.status == 200)
    except Exception:
        # Anything that is not a clean 200 means not ready yet, including a garbled answer from
        # whatever else listens there (http.client raises its own errors for that).
        return False


def cmd_start(args: argparse.Namespace) -> int:
    config = resolve(args)
    state = config.state_dir
    assert state is not None
    pid = instance.holder(state)
    if pid is not None:
        raise CliError(f"already running for {config.workspace}, PID {pid}; use dev-link stop")
    port = _choose_port(config)
    config.server.port = port
    (state / instance.CONNECTION_FILE).unlink(missing_ok=True)
    log_path = state / instance.SERVER_LOG
    argv = [sys.executable, "-m", "dev_link_mcp", "serve", str(config.workspace)]
    argv += ["--state-dir", str(state), "--port", str(port), *_server_flags(args)]
    env = dict(os.environ)
    if args.token is not None:
        # The env layer sits just below the command line, and no --token is passed, so this wins.
        env["DEV_LINK_SERVER__TOKEN"] = args.token
    fd = open_private(log_path)
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT, start_new_session=True, env=env
        )
    finally:
        os.close(fd)
    # The tunnel has its own 30 s to come up before the server starts listening.
    deadline = time.monotonic() + (60 if args.tunnel else 30)
    health = config.base_url() + "/health"
    # Only our child counts: something else answering /health on the port is not a start.
    while True:
        if proc.poll() is not None:
            raise CliError(
                f"the server exited with code {proc.returncode}; last lines of {log_path}:",
                details=instance.tail(log_path, 100),
            )
        if time.monotonic() >= deadline:
            raise CliError(
                f"the server did not answer {health} in time (PID {proc.pid} is still running; dev-link stop"
                f" stops it); last lines of {log_path}:",
                details=instance.tail(log_path, 100),
            )
        if _healthy(health):
            info = instance.read_connection(state)
            if info is not None and info.get("pid") == proc.pid:
                break
        time.sleep(0.2)
    where = where_args(args, config)
    lines = [f"dev-link started for {config.workspace}", "", f"  MCP URL   : {info.get('mcp_url', '?')}"]
    lines.append(f"  dashboard : {info.get('dashboard_url', '?')}")
    for label, key in (("public MCP URL", "public_mcp_url"), ("public dash", "public_dashboard_url")):
        if key in info:
            lines.append(f"  {label:<10}: {info[key]}")
    for name, value in (info.get("headers") or {}).items():
        lines.append(f"  header    : {name}: {value}")
    lines += [
        f"  PID       : {proc.pid}",
        f"  logs      : {log_path}",
        "",
        "The token in these URLs is new on every start; anyone with the URL can use the server.",
    ]
    if "public_mcp_url" in info:
        lines.append("The public URL is reachable from the internet: anyone with it can run commands here.")
    tokens = _tokens(info)
    # Output that is not a terminal tends to end up in CI logs, journals or nohup.out, where a
    # token, and with --tunnel the public URL, would give anyone who reads it the server.
    hide = bool(tokens) and not sys.stdout.isatty()
    if hide:
        lines.append(f"The token is hidden because this output is not a terminal; dev-link url {where} prints the URL.")
    lines += ["", "Commands:", *(f"  dev-link {cmd} {where}" for cmd in ("status", "url", "logs server", "stop"))]
    text = "\n".join(lines)
    print(redact(text, tokens) if hide else text)
    return 0


def _tokens(info: dict) -> list[str]:
    """The token in a connection.json `info`: in the MCP URL path with path-token auth, in the
    Authorization header with bearer; empty with auth none."""
    found = [value.removeprefix("Bearer ") for value in (info.get("headers") or {}).values()]
    path = urlsplit(info.get("mcp_url", "")).path
    if path.startswith("/mcp/"):
        found.append(path.removeprefix("/mcp/"))
    return [token for token in found if token]


def _stop(config: Config) -> None:
    state = config.state_dir
    assert state is not None
    pid = instance.holder(state)
    if pid is None:
        print(f"dev-link is not running for {config.workspace}")
    else:
        print(f"stopping dev-link for {config.workspace} (PID {pid})")
        instance.stop(state, pid)
        print("stopped")
    (state / instance.CONNECTION_FILE).unlink(missing_ok=True)


def cmd_stop(args: argparse.Namespace) -> int:
    _stop(resolve(args))
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    _stop(resolve(args))
    return cmd_start(args)


def cmd_status(args: argparse.Namespace) -> int:
    if args.all:
        return _status_all()
    config = resolve(args)
    assert config.state_dir is not None
    pid = instance.holder(config.state_dir)
    if pid is None:
        print(f"stopped   : {config.workspace}")
        return STATUS_STOPPED
    info = instance.read_connection(config.state_dir) or {}
    lines = [f"running   : PID {pid}", f"workspace : {config.workspace}"]
    for label, key in (
        ("MCP URL", "mcp_url"),
        ("dashboard", "dashboard_url"),
        ("public MCP", "public_mcp_url"),
        ("public dash", "public_dashboard_url"),
    ):
        if key in info:
            lines.append(f"{label:<10}: {info[key]}")
    lines.append(f"state dir : {config.state_dir}")
    print("\n".join(lines))
    return 0


def _status_all() -> int:
    """One line per running instance under the default state root; exit 3 when there is none."""
    root = default_state_root()
    found: list[tuple[str, str, int]] = []
    for state in sorted(p for p in root.glob("*") if p.is_dir()) if root.is_dir() else []:
        pid = instance.holder(state)
        if pid is None:
            continue
        info = instance.read_connection(state) or {}
        port = str(urlsplit(info["mcp_url"]).port) if "mcp_url" in info else "?"
        found.append((str(info.get("workspace", state)), port, pid))
    if not found:
        print(f"no running instances under {root}")
        return STATUS_STOPPED
    print(f"{'PORT':<6} {'PID':<8} WORKSPACE")
    for workspace, port, pid in found:
        print(f"{port:<6} {pid:<8} {workspace}")
    return 0
