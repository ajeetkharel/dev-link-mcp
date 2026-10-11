"""Pieces shared by the command modules: the error type and config resolution."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any

from dev_link_mcp.config import Config, load_config


class CliError(Exception):
    """A failure to report as `dev-link: <message>` on stderr, followed by `details` as they are.

    `code` is the exit status: 2 for invalid settings, 1 for everything else.
    """

    def __init__(self, message: str, *, code: int = 1, details: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or []

    @classmethod
    def from_exc(cls, exc: BaseException, *, code: int = 2) -> CliError:
        """A CliError for an exception whose message has one problem per line, each printed as
        `dev-link: <line>`, as config and tool group errors do."""
        lines = str(exc).splitlines() or [type(exc).__name__]
        return cls(lines[0], code=code, details=[f"dev-link: {line}" for line in lines[1:]])


def config_from_args(args: argparse.Namespace) -> Config:
    """The finalized Config for parsed flags plus the user file, --config and DEV_LINK_* variables.

    Commands that only address an instance have just DIR, --config and --state-dir; the missing
    server flags count as not given. Raises ValueError for any invalid setting (see load_config).
    Like every finalize(), this creates the state dir when it does not exist.
    """
    a = vars(args)
    cli: dict[str, Any] = {
        "workspace": a.get("workspace"),
        "state_dir": a.get("state_dir"),
        "log_level": a.get("log_level"),
        "server": {
            "host": a.get("host"),
            "port": a.get("port"),
            "auth": a.get("auth"),
            "token": a.get("token"),
            "public_url": a.get("public_url"),
            "allowed_hosts": a.get("allowed_host") or [],
        },
        "tunnel": {"ngrok_url": a.get("ngrok_url")},
        "sandbox": {
            "backend": "none" if a.get("no_sandbox") else None,
            "network": False if a.get("no_network") else None,
        },
        "tools": {"enabled": a.get("enable") or [], "disabled": a.get("disable") or []},
    }
    return load_config(a.get("config"), cli)


def resolve(args: argparse.Namespace) -> Config:
    """config_from_args, with invalid settings turned into a CliError with exit status 2."""
    try:
        return config_from_args(args)
    except (ValueError, OSError) as exc:
        raise CliError.from_exc(exc) from None


def where_args(args: argparse.Namespace, config: Config) -> str:
    """The DIR and --config/--state-dir arguments that find this instance again, shell-quoted,
    with a leading space; empty when none are needed. DIR is left out when it is the current
    folder, its default."""
    parts = [] if config.workspace == _cwd() else [str(config.workspace)]
    if vars(args).get("config") is not None:
        parts += ["--config", str(args.config)]
    if vars(args).get("state_dir") is not None:
        parts += ["--state-dir", str(config.state_dir)]
    return f" {shlex.join(parts)}" if parts else ""


def _cwd() -> Path | None:
    try:
        return Path.cwd().resolve()
    except OSError:
        # The current folder was deleted; then DIR is always printed.
        return None
