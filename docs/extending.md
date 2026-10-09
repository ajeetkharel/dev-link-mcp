# Adding tool groups

dev-link's tools come in groups. Another Python package can add its own group as a plugin.
The plugin API is provisional: it may change between 0.x releases.

## The contract

A group is a subclass of `dev_link_mcp.tools.base.ToolGroup`:

```python
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict

from dev_link_mcp.tools.base import ToolGroup


class HelloGroup(ToolGroup):
    name = "hello"

    class Settings(BaseModel):
        model_config = ConfigDict(extra="forbid")
        greeting: str = "Hello"

    settings: Settings

    def register(self, mcp: MCPServer) -> None:
        @mcp.tool()
        def hello(name: str) -> str:
            """Greet someone by name."""
            return f"{self.settings.greeting}, {name}!"

    async def shutdown(self) -> None:
        """Close whatever the group opened. Optional."""
```

- `name` is the group name. `opt_in` (default `False`) only matters for built-in groups.
- `Settings` is a pydantic model for the `groups.<name>` config section. Use `extra="forbid"` so
  typos are reported. The validated instance is `self.settings`.
- `self.rt` gives the config, workspace, sandbox, audit log and process manager.
- dev-link creates one instance per server, calls `register()` once at start and awaits
  `shutdown()` when the server stops. Keep state on the instance, not in module globals.

A complete example lives in [examples/plugin-hello](../examples/plugin-hello).

## Registering the plugin

Declare an entry point in the `dev_link_mcp.tool_groups` group. Its name is the group name:

```toml
[project.entry-points."dev_link_mcp.tool_groups"]
hello = "dev_link_hello:HelloGroup"
```

## Enabling it

Installing a plugin never adds tools by itself. dev-link loads a plugin only when
`tools.enabled` lists its name, and reads its settings from `groups.<name>`:

```yaml
tools:
  enabled: [hello]
groups:
  hello:
    greeting: Hi
```

The server refuses to start when an enabled plugin fails to import, is not a `ToolGroup`
subclass, gets invalid settings, or is installed by more than one package. A plugin with the
name of a built-in group is ignored with a warning, because `tools.enabled` always means the
built-in.

## Trust and the sandbox

Plugins are trusted code. They run inside the dev-link server process, outside the sandbox,
with your user's full access. A tool that runs commands for the agent must run them through
`self.rt.sandbox` (for example `await self.rt.sandbox.run(argv, cwd=...)`) so they stay
confined to the workspace like the built-in tools. Resolve agent-supplied paths with
`self.rt.workspace.resolve(path)` for the same reason.
