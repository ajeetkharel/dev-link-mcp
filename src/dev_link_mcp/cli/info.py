"""Reading a running or stopped instance: url, dashboard and logs."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dev_link_mcp.audit import AuditReader
from dev_link_mcp.cli import instance
from dev_link_mcp.cli.common import CliError, resolve


def cmd_url(args: argparse.Namespace) -> int:
    config = resolve(args)
    assert config.state_dir is not None
    info = instance.connection(config.state_dir, config.workspace)
    print(info.get("public_mcp_url") or info["mcp_url"])
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    config = resolve(args)
    assert config.state_dir is not None
    info = instance.connection(config.state_dir, config.workspace)
    if args.public:
        if "public_dashboard_url" not in info:
            raise CliError(f"the server for {config.workspace} has no public URL (start it with --tunnel)")
        print(info["public_dashboard_url"])
    else:
        print(info["dashboard_url"])
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    config = resolve(args)
    state = config.state_dir
    assert state is not None
    if args.target == "all":
        lines = 50 if args.lines is None else args.lines
        paths = [
            (title, state / name) for title, name in (("server", instance.SERVER_LOG), ("tunnel", instance.TUNNEL_LOG))
        ]
        found = [(title, path) for title, path in paths if path.is_file()]
        if not found:
            raise CliError(f"no server or tunnel log in {state}; they are written by dev-link start")
        for title, path in found:
            print(f"=== {title}: {path} ===")
            print("\n".join(instance.tail(path, lines)))
        return 0
    lines = 100 if args.lines is None else args.lines
    if args.target == "audit":
        return _audit(state, lines, args.follow)
    path = state / (instance.SERVER_LOG if args.target == "server" else instance.TUNNEL_LOG)
    if not path.is_file():
        made_by = "dev-link start" if args.target == "server" else "a server started with --tunnel"
        raise CliError(f"no {args.target} log at {path}; it is written by {made_by}")
    offset = path.stat().st_size
    print("\n".join(instance.tail(path, lines)), flush=True)
    if args.follow:
        for chunk in instance.follow(path, offset):
            sys.stdout.write(chunk)
            sys.stdout.flush()
    return 0


def _audit_line(event: dict[str, Any]) -> str:
    when = datetime.fromtimestamp(event["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    name = event["name"] if event["kind"] == "tool" else f"{event['kind']}:{event['name']}"
    duration = "-" if event["duration_ms"] is None else f"{event['duration_ms']:.0f}ms"
    return f"{when}  {event['client'] or '-'}  {name}  {duration}  {event['status']}"


def _audit(state: Path, lines: int, follow: bool) -> int:
    try:
        reader = AuditReader(state)
    except FileNotFoundError as exc:
        raise CliError(f"{exc}; it is created when the server first starts") from None
    try:
        events = reader.latest(lines)
        for event in events:
            print(_audit_line(event), flush=True)
        if not follow:
            return 0
        # Calls still running are printed again once they finish, so the stream shows every outcome.
        last = events[-1]["id"] if events else reader.last_id()
        running = [e["id"] for e in events if e["status"] == "running"]
        while True:
            time.sleep(1)
            done = [e for e in reader.by_ids(running) if e["status"] != "running"]
            new = reader.after(last)
            if new:
                last = new[-1]["id"]
            running = [i for i in running if i not in {e["id"] for e in done}]
            running += [e["id"] for e in new if e["status"] == "running"]
            for event in [*done, *new]:
                print(_audit_line(event), flush=True)
    finally:
        reader.close()
