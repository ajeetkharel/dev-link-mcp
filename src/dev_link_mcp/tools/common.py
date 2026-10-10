"""Helpers shared by tool modules."""

from __future__ import annotations

import mcp.types as types

READ_ONLY = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
# Changes workspace files but only adds or edits; nothing is lost without a trace.
WRITE = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
# Can delete or overwrite data, or run arbitrary code.
DESTRUCTIVE = types.ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
# Talks to the network or services outside the workspace.
OPEN_WORLD_READ = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
OPEN_WORLD = types.ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def clip(text: str, limit: int) -> str:
    """Keep the head and tail of `text` within `limit` characters, marking the cut."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} characters elided] ...\n\n{text[-half:]}"
