"""`dev-link config show|path|init|schema`."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import yaml

from dev_link_mcp.cli.common import CliError, resolve
from dev_link_mcp.config import default_user_file
from dev_link_mcp.config.loader import SECTIONS
from dev_link_mcp.private import open_private

STARTER = """\
# dev-link user config. Every key is optional; command-line flags and DEV_LINK_* variables
# override what is set here. All settings: docs/configuration.md in the dev-link-mcp repository.
# Uncomment a section together with at least one of its keys.

# server:
#   port: 8765          # unset: `dev-link start` picks the first free port from 8765
#   auth: path-token    # path-token, bearer, or none (loopback only)

# sandbox:
#   network: true       # false blocks network access inside the sandbox

# tools:
#   enabled: [docker]   # opt-in groups and plugins
#   disabled: [browser] # groups to turn off
"""


def _flow(value: Any) -> str:
    """One YAML value on one line."""
    return str(yaml.safe_dump(value, default_flow_style=True, width=1 << 30)).removesuffix("\n...\n").strip()


def cmd_config_show(args: argparse.Namespace) -> int:
    config = resolve(args)
    data = config.model_dump(mode="json")
    sources = config.sources
    if data["server"]["token"]:
        data["server"]["token"] = "***"  # noqa: S105 (a redaction mark, not a password)
        if sources.get("server.token", "default") == "default":
            sources = {**sources, "server.token": "default: random, new on every start"}
    lines = []
    for key, value in data.items():
        if key in SECTIONS:
            lines.append(f"{key}:")
            for sub, sub_value in value.items():
                lines.append(f"  {sub}: {_flow(sub_value)}  # {sources.get(f'{key}.{sub}', 'default')}")
        elif key == "groups":
            lines.append(f"groups:{'' if value else ' {}  # default'}")
            for group, settings in value.items():
                lines.append(f"  {group}:")
                for sub, sub_value in settings.items():
                    lines.append(f"    {sub}: {_flow(sub_value)}  # {sources.get(f'groups.{group}.{sub}', 'default')}")
        else:
            lines.append(f"{key}: {_flow(value)}  # {sources.get(key, 'default')}")
    print("\n".join(lines))
    return 0


def cmd_config_path(args: argparse.Namespace) -> int:
    path = default_user_file()
    print(f"{path} ({'exists' if path.is_file() else 'not created; dev-link config init writes a starter file'})")
    return 0


def cmd_config_init(args: argparse.Namespace) -> int:
    path = default_user_file()
    if path.exists() and not args.force:
        raise CliError(f"{path} already exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(open_private(path), "w") as f:
        f.write(STARTER)
    print(f"wrote {path}")
    return 0


def cmd_config_schema(args: argparse.Namespace) -> int:
    # Loads every tool group class, which the other config commands do not need.
    from dev_link_mcp.config.schema import schema

    print(json.dumps(schema(), indent=2))
    return 0
