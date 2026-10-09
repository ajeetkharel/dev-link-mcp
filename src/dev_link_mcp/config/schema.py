"""JSON Schema of the config file, built from the config and tool group models.

Not imported by the config package itself: it loads every built-in tool group class.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from dev_link_mcp.config.loader import SECTIONS
from dev_link_mcp.config.models import Config
from dev_link_mcp.tools import BUILTINS, load_group_class


def group_settings() -> dict[str, type[BaseModel]]:
    """Settings model of every built-in group, by group name."""
    return {b.name: load_group_class(b.target).Settings for b in BUILTINS}


def schema() -> dict[str, Any]:
    """JSON Schema for a config file, with every default filled in."""
    result = {"$schema": "https://json-schema.org/draft/2020-12/schema", **Config.model_json_schema()}
    result["title"] = "dev-link configuration file"
    # The workspace is normally the command-line argument, so a file without it is valid.
    del result["required"]
    # Fields with a default_factory have no "default" in pydantic's schema.
    for model in SECTIONS.values():
        defaults = model().model_dump(mode="json")
        for name, prop in result["$defs"][model.__name__]["properties"].items():
            prop.setdefault("default", defaults[name])
    # Built-in groups are described in full; any other name may be a plugin, whose keys are unknown here.
    groups = result["properties"]["groups"]
    groups["default"] = {}
    groups["properties"] = {}
    for name, settings in group_settings().items():
        result["$defs"][settings.__name__] = settings.model_json_schema()
        groups["properties"][name] = {"$ref": f"#/$defs/{settings.__name__}"}
    groups["additionalProperties"] = {"type": "object"}
    return result
