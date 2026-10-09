"""Example dev-link plugin: a tool group with one read-only tool and one setting."""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from dev_link_mcp.tools.base import ToolGroup


class HelloGroup(ToolGroup):
    """Greets people by name."""

    name = "hello"

    class Settings(BaseModel):
        model_config = ConfigDict(extra="forbid")

        greeting: str = Field(default="Hello", description="Word put in front of the name.")

    settings: Settings

    def register(self, mcp: MCPServer) -> None:
        greeting = self.settings.greeting

        @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
        def hello(name: str) -> str:
            """Greet someone by name."""
            return f"{greeting}, {name}!"
