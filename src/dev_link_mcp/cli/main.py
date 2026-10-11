"""Argument parsing and dispatch for `dev-link <command>`.

Command modules are imported only when their command runs, so quick commands such as
`dev-link url` do not load the server stack.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

from dev_link_mcp import __version__
from dev_link_mcp.cli.common import CliError
from dev_link_mcp.config import AUTH_MODES

EPILOG = """\
exit status: 0 success, 1 failure, 2 invalid usage or settings, 3 `status` found no running server.
DIR is the workspace folder and defaults to the current directory. Each workspace gets its own
server, state dir and port."""

# Command name -> (module, function) under dev_link_mcp.cli.
_COMMANDS = {
    "serve": ("serve", "cmd_serve"),
    "start": ("serve", "cmd_start"),
    "stop": ("serve", "cmd_stop"),
    "restart": ("serve", "cmd_restart"),
    "status": ("serve", "cmd_status"),
    "url": ("info", "cmd_url"),
    "dashboard": ("info", "cmd_dashboard"),
    "logs": ("info", "cmd_logs"),
    "doctor": ("doctor", "cmd_doctor"),
    "config show": ("config_cmd", "cmd_config_show"),
    "config path": ("config_cmd", "cmd_config_path"),
    "config init": ("config_cmd", "cmd_config_init"),
    "config schema": ("config_cmd", "cmd_config_schema"),
    "tools": ("tools_cmd", "cmd_tools"),
    "install-browser": ("tools_cmd", "cmd_install_browser"),
}


def _add_where(p: argparse.ArgumentParser) -> None:
    """DIR plus the two flags that decide where an instance keeps its state."""
    p.add_argument("workspace", nargs="?", default=".", metavar="DIR", help="workspace folder (default: .)")
    p.add_argument("-c", "--config", type=Path, help="YAML config file")
    p.add_argument("--state-dir", type=Path, help="logs, audit store and sandbox home (outside the workspace)")


def server_parser() -> argparse.ArgumentParser:
    """Parent parser with the server options shared by serve, start, restart, doctor, config show and tools."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("server options")
    g.add_argument("--host", help="bind address (default 127.0.0.1)")
    g.add_argument("--port", type=int, help="port (default 8765; start picks the next free one if unset)")
    g.add_argument("--auth", choices=AUTH_MODES, help="path-token (default), bearer, or none")
    g.add_argument("--token", help="fixed token; default is a new random token every start")
    g.add_argument("--no-sandbox", action="store_true", help="run commands without bubblewrap (full user access)")
    g.add_argument("--no-network", action="store_true", help="block network access inside the sandbox")
    g.add_argument("--enable", action="append", default=[], metavar="GROUP", help="enable an opt-in tool group")
    g.add_argument("--disable", action="append", default=[], metavar="GROUP", help="disable a tool group")
    g.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    g.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST",
        help="extra Host header to accept (exact, or host:* for any port); repeatable",
    )
    g.add_argument("--public-url", help="public base URL behind your own tunnel or proxy")
    g.add_argument(
        "--tunnel",
        action="store_true",
        help="publish the server through a Cloudflare quick tunnel (needs cloudflared; anyone with the URL"
        " can run commands)",
    )
    g.add_argument(
        "--ngrok-url",
        metavar="URL",
        help="with --tunnel, publish through ngrok on your fixed domain (https://name.ngrok-free.app) so the"
        " public URL stays the same across restarts",
    )
    g.add_argument(
        "--new-token",
        action="store_true",
        help="with --tunnel and an ngrok URL, replace the kept token; the old public URL stops working",
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev-link",
        description="Sandboxed development MCP server for one folder.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    server = server_parser()

    def add(name: str, help_text: str, *, with_server: bool = False) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text, parents=[server] if with_server else [])
        _add_where(p)
        return p

    add("serve", "run the server in the foreground", with_server=True)
    add("start", "run the server in the background and wait until it answers", with_server=True)
    add("stop", "stop the server (SIGTERM, then SIGKILL after 10 s)")
    add("restart", "stop, then start with the given options", with_server=True)
    status = add("status", "show whether the server runs (exit 0) or not (exit 3)")
    status.add_argument(
        "--all", action="store_true", help="list every running server under the default state root instead"
    )
    add("url", "print the MCP URL (the public one when there is a tunnel)")
    dashboard = add("dashboard", "print the dashboard URL")
    dashboard.add_argument("--public", action="store_true", help="print the public dashboard URL instead")
    logs = sub.add_parser("logs", help="print server, tunnel or audit logs", description="print logs of one instance")
    logs.add_argument(
        "target", nargs="?", default="all", choices=("server", "tunnel", "audit", "all"), help="default: all"
    )
    _add_where(logs)
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new lines (not with all)")
    logs.add_argument("-n", "--lines", type=int, help="lines to print first (default 100; 50 each for all)")
    add("doctor", "check that the server can run here with these options, and how to fix what fails", with_server=True)

    config = sub.add_parser("config", help="show, locate, create or describe config files")
    config_sub = config.add_subparsers(dest="config_command", metavar="ACTION", required=True)
    show = config_sub.add_parser(
        "show", help="print the effective config with the source of each value", parents=[server]
    )
    _add_where(show)
    config_sub.add_parser("path", help="print the user config file path and whether it exists")
    init = config_sub.add_parser("init", help="write a commented starter user config file")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    config_sub.add_parser("schema", help="print the JSON Schema of config files")

    tools = add("tools", "list tool groups and their tools", with_server=True)
    tools.add_argument("--group", metavar="GROUP", help="only this group")
    tools.add_argument("--json", action="store_true", help="print JSON")
    sub.add_parser(
        "install-browser",
        help="download the Chromium the browser tools use",
        description="download the Chromium the browser tools use",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse a command line. Exits 2 with usage for invalid input, including no command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    if getattr(args, "tunnel", False) and args.public_url:
        parser.error("--tunnel sets the public URL itself; drop --public-url")
    if getattr(args, "ngrok_url", None) and not args.tunnel:
        parser.error("--ngrok-url only applies with --tunnel")
    if args.command == "logs" and args.follow and args.target == "all":
        parser.error("logs all cannot follow; pick server, tunnel or audit with -f")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run one `dev-link` command and return its exit status.

    Failures print `dev-link: <message>` on stderr and raise SystemExit with status 1, or 2
    for invalid settings; argparse exits 2 for invalid usage.
    """
    args = parse_args(argv)
    name = args.command if args.command != "config" else f"config {args.config_command}"
    module_name, function = _COMMANDS[name]
    try:
        module = importlib.import_module(f"dev_link_mcp.cli.{module_name}")
        status: int = getattr(module, function)(args)
    except CliError as exc:
        print(f"dev-link: {exc}", file=sys.stderr)
        for line in exc.details:
            print(line, file=sys.stderr)
        raise SystemExit(exc.code) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except BrokenPipeError:
        # `dev-link logs | head` closes the pipe early; that is not an error. Point stdout at
        # /dev/null so the flush at exit does not fail a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0) from None
    return status
