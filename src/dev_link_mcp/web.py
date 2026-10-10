"""Dashboard routes: static files, the JSON API and the live SSE stream, all under the secret UI path."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, BinaryIO
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from dev_link_mcp.audit import SLICE_DEFAULT, SLICE_FIELDS
from dev_link_mcp.browse import TEXT_DEFAULT, BrowseError, FileBrowser
from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools import Toolset
from dev_link_mcp.tools.tasks import TasksGroup

Endpoint = Callable[[Request], Awaitable[Response]]

# Tool args and results shown on the page are agent-controlled. The page builds DOM only through
# textContent, and this policy limits the damage of a rendering bug: only our own script files
# run, and fetches cannot leave this origin.
PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Raw workspace files: even if a browser opens one directly, nothing in it may run.
RAW_CSP = "default-src 'none'; sandbox"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
_KEEPALIVE_S = 20
_LOG_READ_MAX = 1_000_000


def static_files() -> dict[str, tuple[bytes, str, str]]:
    """Dashboard assets shipped in the package: relative path -> (body, Content-Type, ETag).

    Read once at startup; only files with a known type are included, so the routes built from
    this table are the complete allow-list.
    """
    files: dict[str, tuple[bytes, str, str]] = {}

    def walk(node: Traversable, prefix: str) -> None:
        for child in node.iterdir():
            if child.is_dir():
                if not child.name.startswith(("_", ".")):
                    walk(child, f"{prefix}{child.name}/")
                continue
            ctype = _STATIC_TYPES.get("." + child.name.rpartition(".")[2])
            if ctype is None:
                continue
            body = child.read_bytes()
            files[prefix + child.name] = (body, ctype, '"' + hashlib.sha256(body).hexdigest()[:20] + '"')

    walk(resources.files("dev_link_mcp.dashboard"), "")
    return files


def _int(request: Request, name: str, default: int | None = None) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


async def _chunks(fh: BinaryIO, size: int = 1 << 16) -> AsyncIterator[bytes]:
    """Read `fh` in chunks off the event loop, closing it when done or when the client goes away."""
    try:
        while chunk := await asyncio.to_thread(fh.read, size):
            yield chunk
    finally:
        fh.close()


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})


def _sse(kind: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, default=str)
    if kind == "audit":
        # The id lets the page resume with `after` once it reconnects.
        return f"id: {data['id']}\nevent: audit\ndata: {payload}\n\n"
    return f"event: {kind}\ndata: {payload}\n\n"


def add_dashboard_routes(mcp: MCPServer, rt: Runtime, toolset: Toolset) -> None:
    """Dashboard page, static assets and JSON/SSE API under the secret UI path.

    With auth "none" the path holds no secret, so every dashboard route also checks Host and
    Origin: otherwise any web page the user opens could read the audit log through DNS
    rebinding, or stop processes with a cross-site POST.
    """
    ui = rt.config.ui_path
    api = f"{ui}/api"
    browser = FileBrowser(rt.workspace.root, rt.config.state_dir)

    guard: TransportSecurityMiddleware | None = None
    if rt.config.server.auth == "none":
        # Config validation rejects auth "none" with extra hosts or a public URL, so only loopback remains.
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        guard = TransportSecurityMiddleware(
            TransportSecuritySettings(
                allowed_hosts=hosts,
                allowed_origins=[f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")],
            )
        )

    def route(path: str, method: str = "GET") -> Callable[[Endpoint], Endpoint]:
        def register(handler: Endpoint) -> Endpoint:
            async def endpoint(request: Request) -> Response:
                if guard is not None and (denied := await guard.validate_request(request)) is not None:
                    return denied
                return await handler(request)

            mcp.custom_route(path, methods=[method], include_in_schema=False)(endpoint)
            return handler

        return register

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    # The page uses relative URLs, which only resolve under the trailing slash.
    @route(ui)
    async def bare(_: Request) -> Response:
        return RedirectResponse(f"{ui}/", status_code=307, headers={"Cache-Control": "no-store"})

    def static(body: bytes, ctype: str, etag: str) -> Endpoint:
        headers = {
            "ETag": etag,
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": PAGE_CSP,
        }

        async def serve(request: Request) -> Response:
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
            return Response(body, media_type=ctype, headers=headers)

        return serve

    for rel, (body, ctype, etag) in static_files().items():
        route(f"{ui}/" if rel == "index.html" else f"{ui}/{rel}")(static(body, ctype, etag))

    @route(f"{api}/info")
    async def info(_: Request) -> Response:
        tools = {t.name: t.description for t in await mcp.list_tools()}
        return JSONResponse(
            {
                "workspace": str(rt.workspace.root),
                "state_dir": str(rt.config.state_dir),
                "auth": rt.config.server.auth,
                "sandbox": rt.sandbox.backend.name,
                "network": rt.config.sandbox.network,
                "groups": {
                    group: [{"name": n, "description": tools.get(n, "")} for n in names]
                    for group, names in toolset.tools.items()
                },
                "unavailable": toolset.unavailable,
                "disabled": toolset.disabled,
            }
        )

    @route(f"{api}/tools")
    async def tools_api(_: Request) -> Response:
        listed = {t.name: t for t in await mcp.list_tools()}
        groups = []
        for group, names in toolset.tools.items():
            entries = []
            for name in names:
                tool = listed.get(name)
                if tool is None:
                    continue
                notes = tool.annotations
                entries.append(
                    {
                        "name": name,
                        "title": tool.title or (notes.title if notes else None),
                        "description": tool.description or "",
                        "input_schema": tool.input_schema,
                        "annotations": {
                            "read_only": notes.read_only_hint if notes else None,
                            "destructive": notes.destructive_hint if notes else None,
                            "idempotent": notes.idempotent_hint if notes else None,
                            "open_world": notes.open_world_hint if notes else None,
                        },
                    }
                )
            groups.append({"name": group, "tools": entries})
        return JSONResponse({"groups": groups, "unavailable": toolset.unavailable, "disabled": toolset.disabled})

    @route(f"{api}/events")
    async def events(request: Request) -> Response:
        q = request.query_params
        rows = await asyncio.to_thread(
            rt.audit.query,
            limit=_int(request, "limit", 200) or 200,
            before=_int(request, "before"),
            after=_int(request, "after"),
            kind=q.get("kind"),
            name=q.get("name"),
            status=q.get("status"),
            session=q.get("session"),
            client=q.get("client"),
            text=q.get("q"),
        )
        return JSONResponse(rows)

    @route(f"{api}/events/{{event_id:int}}")
    async def event_detail(request: Request) -> Response:
        event = await asyncio.to_thread(rt.audit.detail, request.path_params["event_id"], SLICE_DEFAULT)
        if event is None:
            return _error(404, "not found")
        return JSONResponse(event)

    @route(f"{api}/events/{{event_id:int}}/result")
    async def event_slice(request: Request) -> Response:
        event_id = request.path_params["event_id"]
        field = request.query_params.get("field") or "result"
        if field not in SLICE_FIELDS:
            return _error(400, f"field must be one of {', '.join(SLICE_FIELDS)}")
        if request.query_params.get("download") == "1":
            text = await asyncio.to_thread(rt.audit.field_text, event_id, field)
            if text is None:
                return _error(404, "not found")
            return Response(
                text,
                media_type="text/plain; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="event-{event_id}-{field}.txt"',
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": RAW_CSP,
                    "Cache-Control": "no-store",
                },
            )
        offset = _int(request, "offset", 0) or 0
        limit = _int(request, "limit", SLICE_DEFAULT) or SLICE_DEFAULT
        part = await asyncio.to_thread(rt.audit.slice, event_id, field, offset, limit)
        if part is None:
            return _error(404, "not found")
        return JSONResponse(part)

    @route(f"{api}/stats")
    async def stats(request: Request) -> Response:
        since = _int(request, "since", 86400) or 86400
        return JSONResponse(await asyncio.to_thread(rt.audit.stats, float(since)))

    @route(f"{api}/stream")
    async def stream(request: Request) -> Response:
        live = rt.audit.live
        queue = live.subscribe()
        rt.processes.watch_logs()

        async def body() -> AsyncIterator[str]:
            try:
                yield "retry: 2000\n\n"
                while not await request.is_disconnected():
                    try:
                        first = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_S)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    # Send whatever else is already queued in the same write.
                    parts = [_sse(*first)]
                    while not queue.empty() and len(parts) < 200:
                        parts.append(_sse(*queue.get_nowait()))
                    yield "".join(parts)
            finally:
                live.unsubscribe(queue)

        return StreamingResponse(
            body(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        )

    @route(f"{api}/processes")
    async def processes(_: Request) -> Response:
        return JSONResponse(rt.processes.list())

    @route(f"{api}/processes/{{pid}}/log")
    async def process_log(request: Request) -> Response:
        pid = request.path_params["pid"]
        tail = _int(request, "tail")
        offset = _int(request, "offset", 0) or 0
        limit = max(1, min(_int(request, "limit", 100_000) or 100_000, _LOG_READ_MAX))
        try:
            if tail is not None:
                text, offset = await asyncio.to_thread(
                    rt.processes.read_log, pid, tail_lines=max(1, tail), max_bytes=_LOG_READ_MAX
                )
            else:
                text, offset = await asyncio.to_thread(rt.processes.read_log, pid, offset=offset, max_bytes=limit)
        except ToolError as exc:  # unknown process id
            return _error(404, str(exc))
        return JSONResponse({"text": text, "offset": offset})

    @route(f"{api}/processes/{{pid}}/stop", method="POST")
    async def process_stop(request: Request) -> Response:
        pid = request.path_params["pid"]
        try:
            code = await rt.processes.stop(pid)
        except ToolError as exc:  # unknown process id
            return _error(404, str(exc))
        rt.audit.record("process", "stopped from dashboard", {"id": pid}, result=f"exit_code={code}")
        return JSONResponse({"exit_code": code})

    @route(f"{api}/tasks")
    async def tasks(_: Request) -> Response:
        group = toolset.groups.get("tasks")
        if not isinstance(group, TasksGroup):
            return JSONResponse({"tasks": [], "notes": []})
        return JSONResponse(await asyncio.to_thread(group.snapshot))

    @route(f"{api}/files")
    async def files(request: Request) -> Response:
        try:
            listing = await asyncio.to_thread(browser.listing, request.query_params.get("path") or ".")
        except BrowseError as exc:
            return _error(exc.status, str(exc))
        except OSError as exc:
            return _error(403, f"cannot read directory: {exc.strerror or type(exc).__name__}")
        return JSONResponse(listing, headers={"Cache-Control": "no-store"})

    @route(f"{api}/files/raw")
    async def files_raw(request: Request) -> Response:
        raw = request.query_params.get("path") or ""
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": RAW_CSP,
            "Cache-Control": "no-store",
        }
        try:
            if request.query_params.get("download") == "1":
                # Streamed from a handle opened without following symlinks; FileResponse would reopen
                # the path, which the agent may have swapped since the check.
                fh, name = await asyncio.to_thread(browser.open_download, raw)
                headers["Content-Length"] = str(os.fstat(fh.fileno()).st_size)
                headers["Content-Disposition"] = f"attachment; filename*=utf-8''{quote(name)}"
                return StreamingResponse(_chunks(fh), media_type="application/octet-stream", headers=headers)
            offset = _int(request, "offset", 0) or 0
            limit = _int(request, "limit", TEXT_DEFAULT) or TEXT_DEFAULT
            chunk = await asyncio.to_thread(browser.read, raw, offset, limit)
        except BrowseError as exc:
            return _error(exc.status, str(exc))
        except OSError as exc:
            return _error(403, f"cannot read file: {exc.strerror or type(exc).__name__}")
        headers.update(
            {
                "X-File-Kind": chunk.kind,
                "X-File-Size": str(chunk.size),
                "X-Offset": str(chunk.offset),
                "X-Next-Offset": str(chunk.next),
            }
        )
        if chunk.kind == "binary":
            headers["Content-Disposition"] = "attachment"
        return Response(chunk.data, media_type=chunk.mime, headers=headers)
