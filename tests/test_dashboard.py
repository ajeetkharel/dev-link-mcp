"""Dashboard page and JSON/SSE API, served by a real uvicorn server on a free port."""

from __future__ import annotations

import asyncio
import json
import re
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from conftest import host_backend
from mcp import Client

import dev_link_mcp.tools as tools
from dev_link_mcp.app import build, connection_info
from dev_link_mcp.config import Config
from dev_link_mcp.runtime import Runtime
from dev_link_mcp.web import static_files

XSS = '<img src=x onerror="alert(1)"><script>alert(2)</script>'


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Served:
    def __init__(self, config: Config, rt: Runtime, groups: dict[str, list[str]], http: httpx.AsyncClient):
        self.config = config
        self.rt = rt
        self.groups = groups
        self.http = http
        self.ui = config.base_url() + config.ui_path
        self.api = self.ui + "/api"
        self.mcp_url = connection_info(config)["mcp_url"]


@pytest.fixture
def serve(tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """`async with serve(modules=("system",), auth="path-token") as s:` runs the full app."""

    @asynccontextmanager
    async def _serve(
        modules: tuple[str, ...] = ("system", "processes", "tasks"),
        auth: str = "path-token",
        disabled: tuple[str, ...] = (),
    ) -> AsyncIterator[Served]:
        monkeypatch.setattr(tools, "BUILTINS", tuple(b for b in tools.BUILTINS if b.name in modules))
        port = _free_port()
        config = Config(
            workspace=workspace,
            state_dir=tmp_path / f"state-{auth}",
            server={"port": port, "auth": auth},
            sandbox={"backend": host_backend()},
            tools={"disabled": list(disabled)},
        ).finalize()
        app, rt, toolset = await build(config)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.02)
            async with httpx.AsyncClient(timeout=10) as http:
                yield Served(config, rt, toolset.tools, http)
        finally:
            server.should_exit = True
            await task
            await rt.processes.shutdown()
            rt.audit.close()

    return _serve


PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


async def test_page_is_served_with_strict_csp_and_no_inline_code(serve):
    async with serve() as s:
        r = await s.http.get(s.ui + "/")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["content-security-policy"] == PAGE_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    html = r.text
    # Boot data now comes from api/info; nothing is injected into the page.
    assert "__DEV_LINK_BOOT__" not in html and "window.DEV_LINK" not in html
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts == ['<script type="module" src="js/main.js">']
    assert re.findall(r"<script[^>]*>(.*?)</script>", html, re.S) == [""]  # no inline script body
    assert " style=" not in html and "<style" not in html
    assert "nonce" not in r.headers["content-security-policy"]
    assert "unsafe" not in r.headers["content-security-policy"]


async def test_bare_ui_path_redirects_to_trailing_slash(serve):
    async with serve() as s:
        r = await s.http.get(s.ui)
    assert r.status_code == 307
    assert r.headers["location"] == s.config.ui_path + "/"


async def test_page_never_inlines_event_data(serve):
    async with serve() as s:
        s.rt.audit.record("tool", "evil", {"x": XSS}, result=XSS, ok=False, error=XSS)
        async with Client(s.mcp_url) as client:
            await client.call_tool("nope", {"payload": XSS})
        html = (await s.http.get(s.ui + "/")).text
        events = (await s.http.get(s.api + "/events")).json()
        sources = [html]
        for rel in static_files():
            sources.append((await s.http.get(f"{s.ui}/{rel}")).text)
    assert any(e["name"] == "nope" for e in events)
    assert len(sources) > 3
    for source in sources:
        assert XSS not in source
        assert "alert(" not in source
        # The page renders server data only through textContent/setAttribute.
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in source


async def test_wrong_token_is_rejected(serve):
    async with serve() as s:
        base = s.config.base_url()
        assert (await s.http.get(base + "/ui/wrongwrongwrongwrongwrong/")).status_code == 404
        assert (await s.http.get(base + "/ui/wrongwrongwrongwrongwrong/api/events")).status_code == 404
        assert (await s.http.get(base + "/ui/")).status_code == 404


async def test_auth_none_serves_plain_ui_path(serve):
    async with serve(modules=("system",), auth="none") as s:
        assert s.config.ui_path == "/ui"
        r = await s.http.get(s.ui + "/")
        info = (await s.http.get(s.api + "/info")).json()
    assert r.status_code == 200
    assert info["auth"] == "none"


async def test_info_lists_groups_with_descriptions(serve):
    async with serve() as s:
        info = (await s.http.get(s.api + "/info")).json()
    assert info["workspace"] == str(s.config.workspace)
    assert info["auth"] == "path-token"
    assert info["sandbox"] == ("bubblewrap" if host_backend() == "auto" else "none")
    assert set(info["groups"]) == set(s.groups)
    names = {t["name"]: t["description"] for t in info["groups"]["system"]}
    assert names.get("workspace_info")


async def test_events_filters_pagination_and_detail(serve):
    async with serve() as s:
        async with Client(s.mcp_url) as client:
            await client.call_tool("server_status", {})
            await client.call_tool("workspace_info", {})
            await client.call_tool("nope", {"needle": "haystack-123"})
        tool_events = (await s.http.get(s.api + "/events", params={"kind": "tool"})).json()
        assert [e["name"] for e in tool_events] == ["nope", "workspace_info", "server_status"]
        assert all("args_preview" in e and "args" not in e and "result" not in e for e in tool_events)

        errors = (await s.http.get(s.api + "/events", params={"status": "error"})).json()
        assert [e["name"] for e in errors] == ["nope"]
        by_name = (await s.http.get(s.api + "/events", params={"name": "server_status"})).json()
        assert len(by_name) == 1
        found = (await s.http.get(s.api + "/events", params={"q": "haystack-123"})).json()
        assert [e["name"] for e in found] == ["nope"]

        first = (await s.http.get(s.api + "/events", params={"kind": "tool", "limit": 2})).json()
        older = (
            await s.http.get(s.api + "/events", params={"kind": "tool", "limit": 2, "before": first[-1]["id"]})
        ).json()
        assert [e["name"] for e in first + older] == ["nope", "workspace_info", "server_status"]

        detail = (await s.http.get(f"{s.api}/events/{errors[0]['id']}")).json()
        assert json.loads(detail["args"]) == {"needle": "haystack-123"}
        assert detail["status"] == "error" and detail["error"]
        ok = (await s.http.get(f"{s.api}/events/{by_name[0]['id']}")).json()
        assert ok["result"]
        missing = await s.http.get(s.api + "/events/999999")
        assert missing.status_code == 404


async def test_token_never_reaches_event_api(serve):
    async with serve() as s:
        s.rt.audit.record("tool", "leak", {"text": f"url /mcp/{s.config.server.token}"}, result=s.config.server.token)
        events = (await s.http.get(s.api + "/events")).text
        leak = next(e for e in json.loads(events) if e["name"] == "leak")
        detail = (await s.http.get(f"{s.api}/events/{leak['id']}")).text
    assert s.config.server.token not in events
    assert s.config.server.token not in detail


async def test_stats_shape(serve):
    async with serve() as s:
        async with Client(s.mcp_url) as client:
            await client.call_tool("server_status", {})
            await client.call_tool("nope", {})
        st = (await s.http.get(s.api + "/stats", params={"since": 3600})).json()
    assert st["calls"] == 2 and st["errors"] == 1 and st["running"] == 0
    assert st["avg_ms"] is not None
    assert {t["name"] for t in st["per_tool"]} == {"server_status", "nope"}
    assert set(st["per_tool"][0]) >= {"name", "calls", "errors", "avg_ms", "max_ms"}
    assert sum(b["calls"] for b in st["timeline"]) == 2
    assert all(b["bucket"] % 300 == 0 for b in st["timeline"])


async def test_stream_sends_start_and_finish_of_an_event(serve):
    async with serve() as s:
        seen: list[dict[str, Any]] = []

        async def listen() -> None:
            async with s.http.stream("GET", s.api + "/stream") as r:
                assert r.headers["content-type"].startswith("text/event-stream")
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        seen.append(json.loads(line[6:]))
                        if len(seen) == 2:
                            return

        listener = asyncio.create_task(listen())
        while not s.rt.audit.live.active:
            await asyncio.sleep(0.02)
        event_id = s.rt.audit.start("tool", "streamed", {"a": 1})
        s.rt.audit.finish(event_id, ok=True, result="done")
        await asyncio.wait_for(listener, 5)
    assert [(e["id"], e["status"]) for e in seen] == [(event_id, "running"), (event_id, "ok")]
    assert seen[0]["args_preview"] == '{"a": 1}'


async def test_processes_list_log_and_stop(serve, workspace):
    async with serve() as s:
        proc = await s.rt.processes.start("echo first; echo second; sleep 30", cwd=workspace, name="srv")
        await s.rt.processes.wait_for(proc.id, pattern="second", timeout_s=10)
        listed = (await s.http.get(s.api + "/processes")).json()
        assert [(p["id"], p["name"], p["status"]) for p in listed] == [(proc.id, "srv", "running")]

        full = (await s.http.get(f"{s.api}/processes/{proc.id}/log", params={"offset": 0})).json()
        assert full["text"] == "first\nsecond\n"
        rest = (await s.http.get(f"{s.api}/processes/{proc.id}/log", params={"offset": 6})).json()
        assert rest == {"text": "second\n", "offset": full["offset"]}
        tail = (await s.http.get(f"{s.api}/processes/{proc.id}/log", params={"tail": 1})).json()
        assert tail["text"] == "second"

        stopped = await s.http.post(f"{s.api}/processes/{proc.id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["exit_code"] is not None
        after = (await s.http.get(s.api + "/processes")).json()
        assert after[0]["status"] != "running"
        audit = (await s.http.get(s.api + "/events", params={"q": "stopped from dashboard"})).json()
        assert audit

        assert (await s.http.get(s.api + "/processes/nope/log")).status_code == 404
        assert (await s.http.post(s.api + "/processes/nope/stop")).status_code == 404
        assert (await s.http.get(f"{s.api}/processes/{proc.id}/stop")).status_code == 405


async def test_tasks_snapshot_route(serve):
    async with serve() as s:
        assert "tasks" in s.groups
        empty = (await s.http.get(s.api + "/tasks")).json()
        assert empty["tasks"] == [] and empty["notes"] == []
        async with Client(s.mcp_url) as client:
            r = await client.call_tool("task_add", {"title": XSS})
            assert not r.is_error
            await client.call_tool("note_write", {"key": "plan", "content": "hello"})
        data = (await s.http.get(s.api + "/tasks")).json()
    assert [t["title"] for t in data["tasks"]] == [XSS]
    assert [(n["key"], n["size"]) for n in data["notes"]] == [("plan", 5)]


async def test_tasks_route_when_group_disabled(serve):
    async with serve(modules=("system", "tasks"), disabled=("tasks",)) as s:
        assert "tasks" not in s.groups
        data = (await s.http.get(s.api + "/tasks")).json()
    assert data == {"tasks": [], "notes": []}


async def test_auth_none_rejects_rebinding_and_cross_site_requests(serve, workspace):
    async with serve(auth="none") as s:
        proc = await s.rt.processes.start("sleep 30", cwd=workspace)
        evil = {"Host": "evil.example"}
        assert (await s.http.get(s.ui + "/", headers=evil)).status_code == 421
        assert (await s.http.get(s.api + "/events", headers=evil)).status_code == 421
        assert (await s.http.get(s.api + "/stream", headers=evil)).status_code == 421
        assert (await s.http.get(f"{s.api}/processes/{proc.id}/log", headers=evil)).status_code == 421

        cross = await s.http.post(f"{s.api}/processes/{proc.id}/stop", headers={"Origin": "http://evil.example"})
        assert cross.status_code == 403
        assert s.rt.processes.get(proc.id).running

        # Extra hosts cannot be combined with auth "none", so only loopback names (any port) pass.
        assert (await s.http.get(s.api + "/events", headers={"Host": "localhost:9"})).status_code == 200
        assert (await s.http.get(s.config.base_url() + "/health", headers=evil)).status_code == 200
        origin = s.config.base_url()
        same = await s.http.post(f"{s.api}/processes/{proc.id}/stop", headers={"Origin": origin})
        assert same.status_code == 200
        assert not s.rt.processes.get(proc.id).running


async def test_token_modes_skip_host_checks(serve):
    # The secret path already stops other sites; LAN and tunnel hostnames must keep working.
    async with serve() as s:
        r = await s.http.get(s.api + "/events", headers={"Host": "lan-box:8765"})
    assert r.status_code == 200


async def test_process_log_edge_queries(serve, workspace):
    async with serve() as s:
        proc = await s.rt.processes.start("printf 'a\\nb\\nc\\n'", cwd=workspace)
        await s.rt.processes.wait_for(proc.id, timeout_s=10)
        log = f"{s.api}/processes/{proc.id}/log"
        size = (await s.http.get(log)).json()["offset"]
        assert (await s.http.get(log, params={"tail": 0})).json()["text"] == "c"
        assert (await s.http.get(log, params={"tail": -3})).json()["text"] == "c"
        past_end = (await s.http.get(log, params={"offset": 10**9})).json()
        assert past_end == {"text": "", "offset": size}
        assert (await s.http.get(log, params={"offset": "junk"})).json()["text"] == "a\nb\nc\n"


# --- static files ---------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


async def test_static_files_are_an_allow_list_with_types_and_etags(serve):
    files = static_files()
    assert {"index.html", "css/app.css", "js/main.js"} <= set(files)
    assert all(rel.endswith((".html", ".css", ".js")) for rel in files)
    async with serve() as s:
        for rel, (body, ctype, etag) in files.items():
            url = s.ui + "/" if rel == "index.html" else f"{s.ui}/{rel}"
            r = await s.http.get(url)
            assert r.status_code == 200, rel
            assert r.content == body
            assert r.headers["content-type"] == ctype
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["cache-control"] == "no-cache"
            assert r.headers["etag"] == etag
            again = await s.http.get(url, headers={"If-None-Match": etag})
            assert again.status_code == 304 and again.content == b""
        assert (await s.http.get(s.ui + "/js/main.js")).headers["content-type"] == "text/javascript; charset=utf-8"
        for missing in ("/js/nope.js", "/index.html", "/__init__.py", "/js/../index.html", "/css/", "/js/main.js.map"):
            assert (await s.http.get(s.ui + missing)).status_code == 404, missing


async def test_every_dashboard_route_checks_host_with_auth_none(serve, workspace):
    (workspace / "a.txt").write_text("hi")
    async with serve(auth="none") as s:
        event_id = s.rt.audit.record("tool", "x", {"a": 1}, result="r")
        evil = {"Host": "evil.example"}
        urls = [
            s.ui,
            s.ui + "/",
            s.ui + "/css/app.css",
            s.ui + "/js/main.js",
            s.api + "/info",
            s.api + "/tools",
            s.api + "/stats",
            s.api + "/tasks",
            s.api + "/processes",
            f"{s.api}/events/{event_id}",
            f"{s.api}/events/{event_id}/result",
            f"{s.api}/events/{event_id}/result?download=1",
            s.api + "/files",
            s.api + "/files/raw?path=a.txt",
            s.api + "/files/raw?path=a.txt&download=1",
        ]
        for url in urls:
            assert (await s.http.get(url, headers=evil)).status_code == 421, url
            assert (await s.http.get(url)).status_code in (200, 307), url


# --- event detail and result slices -----------------------------------------------------------


async def test_event_detail_sends_first_slice_and_sizes(serve):
    big = "x" * 70000 + "END"
    async with serve() as s:
        event_id = s.rt.audit.record("tool", "big", {"k": "v"}, result=big)
        quiet = s.rt.audit.record("tool", "quiet")
        detail = (await s.http.get(f"{s.api}/events/{event_id}")).json()
        bare = (await s.http.get(f"{s.api}/events/{quiet}")).json()
    assert detail["result_size"] == len(big) and len(detail["result"]) == 65536
    assert detail["args"] == '{"k": "v"}' and detail["args_size"] == len('{"k": "v"}')
    assert detail["error"] is None and detail["error_size"] == 0
    assert detail["name"] == "big" and detail["status"] == "ok" and detail["result_bytes"] == len(big)
    assert bare["result"] is None and bare["result_size"] == 0 and bare["args"] is None


async def test_result_slices_count_characters_and_never_split_them(serve):
    text = "aé€😀" * 1000  # 1, 2, 3 and 4 UTF-8 bytes per character
    async with serve() as s:
        event_id = s.rt.audit.record("tool", "uni", {"q": "ü"}, ok=False, result=text, error="boom")
        url = f"{s.api}/events/{event_id}/result"
        first = (await s.http.get(url, params={"offset": 0, "limit": 3})).json()
        assert first == {"field": "result", "offset": 0, "text": "aé€", "size": 4000, "next": 3}
        mid = (await s.http.get(url, params={"offset": 3, "limit": 2})).json()
        assert mid["text"] == "😀a" and mid["next"] == 5
        parts, offset = [], 0
        while True:
            part = (await s.http.get(url, params={"offset": offset, "limit": 777})).json()
            if not part["text"]:
                break
            parts.append(part["text"])
            offset = part["next"]
        assert "".join(parts) == text
        assert (await s.http.get(url, params={"offset": 10**9})).json()["text"] == ""
        assert (await s.http.get(url, params={"offset": -5, "limit": 1})).json()["text"] == "a"
        default = (await s.http.get(url)).json()
        assert default["offset"] == 0 and len(default["text"]) == 4000
        assert (await s.http.get(url, params={"field": "args"})).json()["text"] == '{"q": "\\u00fc"}'
        assert (await s.http.get(url, params={"field": "error"})).json()["text"] == "boom"
        assert (await s.http.get(url, params={"field": "session"})).status_code == 400
        assert (await s.http.get(f"{s.api}/events/999999/result")).status_code == 404
        assert (await s.http.get(f"{s.api}/events/999999/result", params={"download": 1})).status_code == 404

        raw = await s.http.get(url, params={"download": 1})
        assert raw.status_code == 200
        assert raw.text == text
        assert raw.headers["content-type"] == "text/plain; charset=utf-8"
        assert raw.headers["content-disposition"] == f'attachment; filename="event-{event_id}-result.txt"'
        assert raw.headers["x-content-type-options"] == "nosniff"


async def test_result_slice_limit_is_capped(serve):
    async with serve() as s:
        s.rt.audit._result_bytes = 3_000_000
        event_id = s.rt.audit.record("tool", "huge", result="y" * 2_000_000)
        part = (await s.http.get(f"{s.api}/events/{event_id}/result", params={"limit": 10**9})).json()
    assert len(part["text"]) == 1048576 and part["size"] == 2_000_000


# --- keyset pagination ----------------------------------------------------------------------


async def test_before_and_after_pages_with_filters(serve):
    async with serve() as s:
        audit = s.rt.audit
        ids = [audit.record("tool", "a" if i % 2 else "b", {"i": i}, ok=i % 3 != 0) for i in range(12)]
        a_ids = [i for n, i in enumerate(ids) if n % 2]

        async def page(**params: Any) -> list[int]:
            return [e["id"] for e in (await s.http.get(s.api + "/events", params=params)).json()]

        newest = await page(name="a", limit=2)
        assert newest == a_ids[::-1][:2]
        older = await page(name="a", limit=2, before=newest[-1])
        assert older == a_ids[::-1][2:4]
        # `after` pages forward in ascending order, for catching up.
        assert await page(name="a", after=a_ids[1], limit=2) == a_ids[2:4]
        assert await page(name="a", after=a_ids[-1]) == []
        assert await page(after=0, limit=3) == sorted(await page(limit=1000))[:3]
        errors_after = await page(status="error", after=ids[2])
        assert errors_after == [i for n, i in enumerate(ids) if n % 3 == 0 and i > ids[2]]
        assert await page(q='"i": 7', after=ids[0]) == [ids[7]]
        assert await page(kind="tool", name="b", status="ok", before=ids[-1], after=ids[3]) == [
            i for n, i in enumerate(ids) if n % 2 == 0 and n % 3 != 0 and ids[3] < i < ids[-1]
        ]


# --- live stream ----------------------------------------------------------------------------


async def _collect(s: Served, count: int, kinds: set[str]) -> tuple[asyncio.Task[list[dict[str, Any]]], None]:
    async def listen() -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []
        kind, event_id = "message", None
        async with s.http.stream("GET", s.api + "/stream") as r:
            async for line in r.aiter_lines():
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("id: "):
                    event_id = line[4:]
                elif line.startswith("data: "):
                    if kind in kinds:
                        seen.append({"type": kind, "id": event_id, "data": json.loads(line[6:])})
                    if len(seen) == count:
                        return seen
                elif line == "":
                    kind, event_id = "message", None
        return seen

    task = asyncio.create_task(listen())
    while not s.rt.audit.live.active:
        await asyncio.sleep(0.02)
    return task, None


async def test_stream_types_audit_messages_with_ids(serve):
    async with serve() as s:
        task, _ = await _collect(s, 2, {"audit"})
        event_id = s.rt.audit.start("tool", "typed", {"a": 1})
        s.rt.audit.finish(event_id, ok=True)
        seen = await asyncio.wait_for(task, 5)
    assert [(m["id"], m["data"]["status"]) for m in seen] == [(str(event_id), "running"), (str(event_id), "ok")]


async def test_stream_sends_process_and_log_messages(serve, workspace):
    async with serve() as s:
        task, _ = await _collect(s, 1, {"process"})
        logs, _ = await _collect(s, 1, {"log"})
        # The first size check may run before the output is written; wait for the growth.
        proc = await s.rt.processes.start("sleep 0.3; echo hello; sleep 30", cwd=workspace, name="talker")
        [started] = await asyncio.wait_for(task, 10)
        [first] = await asyncio.wait_for(logs, 10)
        grown, _ = await _collect(s, 1, {"log"})
        if first["data"]["offset"] == 0:
            [first] = await asyncio.wait_for(grown, 10)
        else:
            grown.cancel()
        await s.rt.processes.stop(proc.id)
    assert started["data"]["change"] == "started" and started["data"]["id"] == proc.id
    assert started["data"]["name"] == "talker"
    assert first["data"] == {"id": proc.id, "offset": len("hello\n")}


async def test_stream_reports_process_exit_and_stop(serve, workspace):
    async with serve() as s:
        task, _ = await _collect(s, 4, {"process"})
        quick = await s.rt.processes.start("true", cwd=workspace)
        slow = await s.rt.processes.start("sleep 30", cwd=workspace)
        await s.rt.processes.wait_for(quick.id, timeout_s=10)
        await s.rt.processes.stop(slow.id)
        seen = await asyncio.wait_for(task, 10)
    changes = {(m["data"]["id"], m["data"]["change"]) for m in seen}
    assert changes == {(quick.id, "started"), (quick.id, "exited"), (slow.id, "started"), (slow.id, "stopped")}


async def test_stream_sends_tasks_messages(serve):
    async with serve() as s:
        task, _ = await _collect(s, 2, {"tasks"})
        async with Client(s.mcp_url) as client:
            await client.call_tool("task_add", {"title": "one"})
            await client.call_tool("note_write", {"key": "k", "content": "v"})
        seen = await asyncio.wait_for(task, 5)
    assert [m["data"] for m in seen] == [{"change": "task"}, {"change": "note"}]


async def test_no_log_watch_without_subscribers(serve, workspace):
    async with serve() as s:
        proc = await s.rt.processes.start("sleep 30", cwd=workspace)
        assert s.rt.processes._log_watch is None
        await s.rt.processes.stop(proc.id)


# --- clients and tools ----------------------------------------------------------------------


async def test_clients_counts_sessions_and_stateless_clients(serve):
    async with serve() as s:
        audit = s.rt.audit
        audit.record("tool", "a", session="s1", client="claude 1")
        audit.record("tool", "a", session="s1", client="claude 1")
        audit.record("tool", "a", session="s2", client="claude 1")
        audit.record("tool", "a", client="stateless 2")
        audit.record("tool", "a", client="stateless 2")
        audit.record("process", "p1", client="ignored")
        st = (await s.http.get(s.api + "/stats", params={"since": 3600})).json()
    assert st["clients"] == 3


async def test_tools_api_lists_schemas_annotations_and_inactive_groups(serve):
    async with serve(modules=("system", "processes", "tasks", "browser"), disabled=("tasks",)) as s:
        data = (await s.http.get(s.api + "/tools")).json()
        info = (await s.http.get(s.api + "/info")).json()
    groups = {g["name"]: g["tools"] for g in data["groups"]}
    assert set(groups) == set(s.groups)
    start = next(t for t in groups["processes"] if t["name"] == "start_process")
    assert start["input_schema"]["properties"]["command"]["type"] == "string"
    assert "command" in start["input_schema"]["required"]
    assert set(start["annotations"]) == {"read_only", "destructive", "idempotent", "open_world"}
    status = next(t for t in groups["system"] if t["name"] == "server_status")
    assert status["annotations"]["read_only"] is True and status["description"]
    assert "tasks" in data["disabled"] and info["disabled"] == data["disabled"]
    assert data["unavailable"] == info["unavailable"]


# --- files ----------------------------------------------------------------------------------


@pytest.fixture
def tree(workspace: Path, tmp_path: Path) -> Path:
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print('hi')\n")
    (workspace / "Zeta.txt").write_text("z")
    (workspace / "alpha.md").write_text("# a\n")
    (workspace / "page.html").write_text("<script>alert(1)</script>")
    (workspace / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    (workspace / "shot.png").write_bytes(PNG)
    (workspace / "named.txt").write_bytes(PNG)  # an image whatever the name says
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    (workspace / "latin1.txt").write_bytes("café".encode("latin-1"))
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]\n")
    (workspace / "sub" / ".git").mkdir(parents=True)
    (workspace / "sub" / ".git" / "HEAD").write_text("ref")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (workspace / "escape").symlink_to(outside)
    (workspace / "escape-dir").symlink_to(tmp_path)
    (workspace / "inner").symlink_to(workspace / "src")
    (workspace / "to-git").symlink_to(workspace / ".git" / "config")
    (workspace / "dangling").symlink_to(workspace / "nope")
    (workspace / "loop-a").symlink_to(workspace / "loop-b")
    (workspace / "loop-b").symlink_to(workspace / "loop-a")
    return workspace


async def test_files_listing(serve, tree):
    async with serve() as s:
        root = (await s.http.get(s.api + "/files")).json()
        sub = (await s.http.get(s.api + "/files", params={"path": "src"})).json()
        via_link = (await s.http.get(s.api + "/files", params={"path": "inner"})).json()
        absolute = (await s.http.get(s.api + "/files", params={"path": str(tree / "src")})).json()
    names = [e["name"] for e in root["entries"]]
    assert ".git" not in names
    # Directories (and links to directories) first, then case-insensitive by name.
    assert names[:3] == ["inner", "src", "sub"]
    assert names[3:] == sorted(names[3:], key=str.lower)
    by_name = {e["name"]: e for e in root["entries"]}
    assert by_name["src"]["type"] == "dir"
    assert by_name["alpha.md"]["type"] == "file" and by_name["alpha.md"]["size"] == 4
    assert by_name["escape"] == {**by_name["escape"], "type": "symlink", "followable": False, "target": None}
    assert by_name["escape-dir"]["followable"] is False
    assert by_name["to-git"]["followable"] is False
    assert by_name["dangling"]["followable"] is False
    assert by_name["loop-a"]["followable"] is False
    assert by_name["inner"]["followable"] is True and by_name["inner"]["target"] == "dir"
    assert root["path"] == "." and root["truncated"] is False and root["total"] == len(names)
    assert [e["name"] for e in sub["entries"]] == ["main.py"] and sub["path"] == "src"
    assert [e["name"] for e in via_link["entries"]] == ["main.py"]
    assert absolute["path"] == "src"


async def test_files_listing_is_capped(serve, workspace):
    for i in range(2005):
        (workspace / f"f{i:05}").write_text("")
    async with serve() as s:
        listing = (await s.http.get(s.api + "/files")).json()
    assert listing["truncated"] is True and listing["total"] == 2005 and len(listing["entries"]) == 2000


async def test_files_refuses_escapes_git_and_state(serve, tree, tmp_path):
    async with serve() as s:
        state = s.config.state_dir
        assert state is not None
        refused = [
            "..",
            "../outside.txt",
            "src/../../outside.txt",
            str(tmp_path / "outside.txt"),
            "/etc/passwd",
            "~/.bashrc",
            "escape",
            "escape-dir",
            "escape-dir/outside.txt",
            ".git",
            ".git/config",
            "sub/.git/HEAD",
            "to-git",
            "src/../.git/config",
            str(state),
            str(state / "audit.sqlite3"),
        ]
        for path in refused:
            for route in ("/files", "/files/raw", "/files/raw?download=1"):
                url = s.api + route
                r = await s.http.get(url, params={"path": path})
                assert r.status_code == 403, (route, path, r.text)
                assert "error" in r.json()
        # Symlink loops raise before Python 3.13 (refused) and resolve to nothing after (not found).
        for path in ("loop-a", "loop-b/x"):
            r = await s.http.get(s.api + "/files/raw", params={"path": path})
            assert r.status_code in (403, 404) and "error" in r.json()
        for path in ("nope.txt", "src/nope", "dangling"):
            r = await s.http.get(s.api + "/files/raw", params={"path": path})
            assert r.status_code == 404 and "error" in r.json()
        assert (await s.http.get(s.api + "/files/raw", params={"path": "src"})).status_code == 400
        assert (await s.http.get(s.api + "/files", params={"path": "alpha.md"})).status_code == 400
        assert (await s.http.get(s.api + "/files/raw")).status_code == 400


async def test_files_refuse_outside_paths_even_without_sandbox(serve, tree, tmp_path, monkeypatch):
    async with serve() as s:
        s.rt.workspace.confined = False  # what --no-sandbox sets for the agent's own tools
        r = await s.http.get(s.api + "/files/raw", params={"path": str(tmp_path / "outside.txt")})
    assert r.status_code == 403


RAW_HEADERS = {"x-content-type-options": "nosniff", "content-security-policy": "default-src 'none'; sandbox"}


async def test_files_raw_types_are_sniffed_and_never_active(serve, tree):
    async with serve() as s:

        async def raw(path: str, **params: Any) -> httpx.Response:
            r = await s.http.get(s.api + "/files/raw", params={"path": path, **params})
            assert r.status_code == 200, r.text
            for k, v in RAW_HEADERS.items():
                assert r.headers[k] == v
            return r

        text = await raw("src/main.py")
        assert text.headers["content-type"] == "text/plain; charset=utf-8"
        assert text.headers["x-file-kind"] == "text" and text.text == "print('hi')\n"
        assert (text.headers["x-file-size"], text.headers["x-offset"], text.headers["x-next-offset"]) == (
            "12",
            "0",
            "12",
        )
        for active in ("page.html", "pic.svg"):
            r = await raw(active)
            assert r.headers["content-type"] == "text/plain; charset=utf-8", active
        png = await raw("shot.png")
        assert png.headers["content-type"] == "image/png" and png.content == PNG
        assert (await raw("named.txt")).headers["content-type"] == "image/png"
        for binary in ("blob.bin", "latin1.txt"):
            r = await raw(binary)
            assert r.headers["content-type"] == "application/octet-stream"
            assert r.headers["content-disposition"] == "attachment"
            assert r.headers["x-file-kind"] == "binary"
        linked = await raw("inner/main.py")
        assert linked.text == "print('hi')\n"
        download = await raw("page.html", download=1)
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.content == b"<script>alert(1)</script>"


async def test_files_raw_ranges_respect_utf8_and_caps(serve, workspace):
    text = ("aé€😀\n" * 100_000).encode()  # 1_100_000 bytes
    (workspace / "big.txt").write_bytes(text)
    async with serve() as s:
        url = s.api + "/files/raw"
        first = await s.http.get(url, params={"path": "big.txt"})
        assert first.headers["x-file-size"] == str(len(text))
        assert len(first.content) <= 256 * 1024 and len(first.content) > 256 * 1024 - 4
        first.content.decode("utf-8")  # whole characters only
        nxt = int(first.headers["x-next-offset"])
        assert nxt == len(first.content)
        # An offset inside a character moves forward to the next whole one.
        mid = await s.http.get(url, params={"path": "big.txt", "offset": 2, "limit": 10})
        assert int(mid.headers["x-offset"]) == 3 and mid.content.decode("utf-8").startswith("€")
        capped = await s.http.get(url, params={"path": "big.txt", "limit": 10**9})
        assert len(capped.content) <= 1024 * 1024
        parts, offset = [], 0
        while offset < len(text):
            r = await s.http.get(url, params={"path": "big.txt", "offset": offset, "limit": 300_001})
            assert int(r.headers["x-offset"]) == offset
            parts.append(r.content)
            offset = int(r.headers["x-next-offset"])
        assert b"".join(parts) == text
        tail = await s.http.get(url, params={"path": "big.txt", "offset": 10**12})
        assert tail.content == b"" and tail.headers["x-offset"] == str(len(text))


async def test_files_raw_oversize_image_is_binary(serve, workspace, monkeypatch):
    import dev_link_mcp.browse as browse

    monkeypatch.setattr(browse, "IMAGE_MAX", 10)
    (workspace / "huge.png").write_bytes(PNG)
    async with serve() as s:
        r = await s.http.get(s.api + "/files/raw", params={"path": "huge.png", "limit": 8})
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-file-kind"] == "binary" and r.content == PNG[:8]


async def test_files_raw_refuses_fifos(serve, workspace):
    import os

    os.mkfifo(workspace / "pipe")
    async with serve() as s:
        listing = (await s.http.get(s.api + "/files")).json()
        r = await s.http.get(s.api + "/files/raw", params={"path": "pipe"})
    assert {e["name"]: e["type"] for e in listing["entries"]}["pipe"] == "other"
    assert r.status_code == 400
