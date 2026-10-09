"""MCP request middleware that writes every tool call to the audit store."""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

from dev_link_mcp.audit import AuditStore

_CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
# Handshake requests: `initialize` on 2025-era protocols, `server/discover` on 2026-07-28+.
_HANDSHAKES = ("initialize", "server/discover")


def _client_label(info: Any) -> str | None:
    if not isinstance(info, dict) or not info.get("name"):
        return None
    return f"{info['name']} {info.get('version', '')}".strip()


def result_text(result: HandlerResult) -> tuple[bool, str]:
    """(is_error, text) for a tools/call result in any of the shapes a handler may return."""
    if isinstance(result, types.CallToolResult):
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(f"[{getattr(block, 'type', 'content')} block]")
        if not parts and result.structured_content is not None:
            parts.append(json.dumps(result.structured_content, default=str, indent=2))
        return result.is_error, "\n".join(parts)
    if isinstance(result, dict):
        # SDK-generated errors (unknown tool, bad arguments) arrive as a serialized CallToolResult.
        if isinstance(result.get("content"), list):
            try:
                return result_text(types.CallToolResult.model_validate(result))
            except ValueError:
                pass
        return bool(result.get("isError") or result.get("is_error")), json.dumps(result, default=str)
    if result is None:
        return False, ""
    return False, result.model_dump_json()


class AuditMiddleware:
    """Records tools/call requests (args, outcome, duration) and client handshakes.

    Session ids come from the `mcp-session-id` header (stateful 2025-era HTTP);
    stateless 2026-07-28 clients have none, so their events carry only the client name.
    """

    def __init__(self, audit: AuditStore):
        self.audit = audit
        self._clients: dict[str, str] = {}

    def _identify(self, ctx: ServerRequestContext[Any, Any]) -> tuple[str | None, str | None]:
        headers = getattr(ctx.request, "headers", None)
        session = headers.get("mcp-session-id") if headers is not None else None
        params = ctx.params or {}
        client = _client_label((params.get("_meta") or {}).get(_CLIENT_INFO_META))
        if client is None and session:
            client = self._clients.get(session)
        if client is None and headers is not None:
            client = headers.get("user-agent")
        return session, client

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method in _HANDSHAKES:
            result = await call_next(ctx)
            session, client = self._identify(ctx)
            params = dict(ctx.params or {})
            client = _client_label(params.get("clientInfo")) or client
            if ctx.method == "initialize" and session is None:
                # The session id is minted during initialize and only appears on later requests.
                session = getattr(ctx.session, "session_id", None)
            if session and client:
                self._clients[session] = client
            params.pop("_meta", None)
            self.audit.record(
                "session", ctx.method, params, session=session, client=client, result=f"protocol {ctx.protocol_version}"
            )
            return result
        if ctx.method != "tools/call":
            return await call_next(ctx)

        call = ctx.params or {}
        session, client = self._identify(ctx)
        event_id = self.audit.start(
            "tool", str(call.get("name", "?")), call.get("arguments") or {}, session=session, client=client
        )
        try:
            result = await call_next(ctx)
        except BaseException as exc:
            # BaseException so client cancellations are recorded too; it is re-raised unchanged.
            self.audit.finish(event_id, ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        is_error, text = result_text(result)
        self.audit.finish(event_id, ok=not is_error, result=text, error=text[:2000] if is_error else None)
        return result
