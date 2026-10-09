"""Sandbox backends: what confines agent commands, and how the configured one is chosen.

A backend only turns a command into the argv that runs it confined. Environment building,
timeouts, output capture and process cleanup stay in the Sandbox facade.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from dev_link_mcp.config import Config
from dev_link_mcp.sandbox.backends.bubblewrap import Bubblewrap
from dev_link_mcp.sandbox.backends.none import Unconfined

log = logging.getLogger(__name__)

_OPT_OUT = "or pass --no-sandbox (sandbox.backend: none) to run agent commands with your full user access"


class SandboxUnavailable(RuntimeError):
    """The configured sandbox cannot run on this machine; the server must not start."""


class Backend(Protocol):
    name: str
    # False means commands run with the user's own access, so they get the host environment.
    confined: bool

    def available(self) -> str | None:
        """None when commands can run, else a one-line reason with a fix hint."""
        ...

    def visible(self, path: Path) -> bool:
        """Whether a host path can be seen by commands; decides which PATH entries are kept."""
        ...

    def wrap(self, argv: Sequence[str], cwd: Path, extra_env: Mapping[str, str]) -> list[str]:
        """The argv that runs `argv` confined, in `cwd`, with `extra_env` set only for the command."""
        ...


def select(config: Config, *, home: Path, tmp: Path) -> Backend:
    """The backend for `config.sandbox.backend`.

    `home` and `tmp` are the private home and /tmp directories prepared by the Sandbox. `none`
    is always allowed and logs a warning. `auto` and `bubblewrap` raise SandboxUnavailable when
    bubblewrap cannot run here; there is no silent fallback to `none`.
    """
    if config.sandbox.backend == "none":
        log.warning("sandbox backend is none: agent commands run with your full user access")
        return Unconfined()
    backend = Bubblewrap(config, home=home, tmp=tmp)
    reason = backend.available()
    if reason is not None:
        raise SandboxUnavailable(f"cannot start the {backend.name} sandbox: {reason}; {_OPT_OUT}")
    return backend
