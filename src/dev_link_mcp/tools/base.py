"""The tool group contract shared by built-in groups and third-party plugins.

Provisional: this API may change between 0.x releases.
"""

from __future__ import annotations

from typing import ClassVar

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict

from dev_link_mcp.runtime import Runtime


class NoSettings(BaseModel):
    """Settings of a group that has none: any key under `groups.<name>` is an error."""

    model_config = ConfigDict(extra="forbid")


class ToolGroup:
    """A named set of MCP tools with its own settings and lifecycle.

    The server creates one instance per server, calls register() once, and awaits shutdown()
    when it stops. Keep all state on the instance, never in module globals, so two servers in
    one process do not share it.

    Subclasses set:
    - `name`: the group name used in `tools.enabled`, `tools.disabled` and `groups.<name>`.
    - `opt_in`: True if the group stays off unless `tools.enabled` lists it.
    - `Settings`: a pydantic model for the `groups.<name>` config section. Use
      `extra="forbid"` so typos are reported.
    """

    name: ClassVar[str]
    opt_in: ClassVar[bool] = False
    Settings: ClassVar[type[BaseModel]] = NoSettings

    def __init__(self, rt: Runtime, settings: BaseModel) -> None:
        """`rt` holds the config, workspace, sandbox, audit log and process manager of this server.
        `settings` is the validated `groups.<name>` section, an instance of `Settings`."""
        self.rt = rt
        self.settings = settings

    def register(self, mcp: MCPServer) -> None:
        """Add this group's tools to `mcp` with `@mcp.tool`. Called once, before the server starts."""
        raise NotImplementedError

    async def shutdown(self) -> None:
        """Release what the group holds (browsers, connections, temp files). Called once when the
        server stops, even if no tool was ever used. The default does nothing."""
