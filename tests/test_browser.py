import asyncio
import base64
import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import mcp.types as types
import pytest
from conftest import build_server, text
from mcp import Client
from pydantic import ValidationError

from dev_link_mcp.tools import browser

pytestmark = pytest.mark.browser

INDEX = """<!doctype html><html><head><title>Demo Form</title></head><body>
<h1>Welcome to the demo</h1>
<form action="/submit" method="get">
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <input name="pw" type="password" value="s3cret-value">
  <select id="color" name="color"><option value="r">Red</option><option value="g">Green</option></select>
  <button type="submit">Send</button>
</form>
<a href="/next">Next page</a>
<a href="/next" target="_blank" id="popup">Popup</a>
<div style="display:none"><button>Hidden button</button></div>
<script>
  console.log("hello from page");
  console.error("boom");
  fetch("/missing");
</script>
</body></html>"""

NEXT = "<!doctype html><html><head><title>Next</title></head><body><h1 id='next'>Second page</h1></body></html>"
LATE = """<!doctype html><html><head><title>Late</title></head><body>
<script>setTimeout(() => { const d = document.createElement('p'); d.id = 'late'; d.textContent = 'Arrived';
document.body.appendChild(d); }, 300);</script></body></html>"""
EXTERNAL = """<!doctype html><html><head><title>External</title></head><body>
<script>fetch("http://203.0.113.7/x").catch(() => console.log("blocked"));</script></body></html>"""
CHATTY = """<!doctype html><html><body><script>
for (let i = 0; i < 30; i++) console.error("e" + i + " " + "x".repeat(20000));</script></body></html>"""
THROWS = "<!doctype html><html><body><script>throw new Error('uncaught kaboom')</script></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.hits.append(self.path)
        parts = urlsplit(self.path)
        pages = {"/": INDEX, "/next": NEXT, "/late": LATE, "/external": EXTERNAL, "/throws": THROWS}
        pages["/chatty"] = CHATTY
        if parts.path == "/slow":
            time.sleep(3)
        if parts.path == "/redirect-out":
            self.send_response(302)
            self.send_header("Location", "http://203.0.113.7/landing")
            self.end_headers()
            return
        if parts.path in pages:
            body, status = pages[parts.path], 200
        elif parts.path == "/submit":
            q = parse_qs(parts.query)
            body = f"<html><head><title>Done</title></head><body>Thanks {q.get('email', [''])[0]}"
            body += f" color={q.get('color', [''])[0]}</body></html>"
            status = 200
        else:
            body, status = "not here", 404
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Request paths seen, so a test can prove a blocked request never reached the server.
    hits: list[str]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hits = []


@pytest.fixture(scope="module")
def site_server() -> Iterator[_Server]:
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def site(site_server: _Server) -> str:
    return f"http://127.0.0.1:{site_server.server_address[1]}"


async def open_page(client, url, **kwargs) -> dict:
    r = await client.call_tool("browser_open", {"url": url, **kwargs})
    assert not r.is_error, text(r)
    return r.structured_content


async def call(client, name, args=None):
    r = await client.call_tool(name, args or {})
    assert not r.is_error, text(r)
    return r


async def test_open_reports_status_title_and_errors(connect, site):
    async with connect("browser") as client:
        res = await open_page(client, site + "/")
        assert res["page_id"].startswith("p")
        assert res["title"] == "Demo Form"
        assert res["status"] == 200
        errors = "\n".join(res["new_console_errors"])
        assert "boom" in errors


async def test_open_404_status(connect, site):
    async with connect("browser") as client:
        res = await open_page(client, site + "/nope")
        assert res["status"] == 404


async def test_snapshot_lists_fields_buttons_links_and_hides_passwords(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        out = text(await call(client, "browser_snapshot", {"page_id": pid}))
    assert "Demo Form" in out
    assert "-> #email" in out
    assert "label='Email'" in out
    assert "-> #color" in out
    assert "'Send'" in out
    assert "Hidden button" not in out
    assert "href=/next" in out
    assert "Welcome to the demo" in out
    assert "s3cret-value" not in out


async def test_form_flow_type_select_click_and_wait(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        await call(client, "browser_type", {"page_id": pid, "selector": "#email", "text": "a@b.co"})
        sel = await call(client, "browser_select", {"page_id": pid, "selector": "#color", "value": "Green"})
        assert sel.structured_content["selected"] == ["g"]
        await call(client, "browser_click", {"page_id": pid, "selector": "button[type=submit]"})
        waited = await call(client, "browser_wait", {"page_id": pid, "text": "Thanks a@b.co"})
        assert "/submit?" in waited.structured_content["url"]
        assert "color=g" in text(await call(client, "browser_snapshot", {"page_id": pid}))


async def test_type_with_submit_presses_enter(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        await call(client, "browser_type", {"page_id": pid, "selector": "#email", "text": "x@y.z", "submit": True})
        res = await call(client, "browser_wait", {"page_id": pid, "url_pattern": "**/submit?**"})
        assert res.structured_content["title"] == "Done"


async def test_press_key_moves_focus(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        await call(client, "browser_click", {"page_id": pid, "selector": "#email"})
        await call(client, "browser_press", {"page_id": pid, "key": "Tab"})
        r = await call(client, "browser_eval", {"page_id": pid, "script": "document.activeElement.name"})
        assert text(r) == '"pw"'
        await call(client, "browser_press", {"page_id": pid, "key": "a", "selector": "#email"})
        r = await call(client, "browser_eval", {"page_id": pid, "script": "document.querySelector('#email').value"})
        assert text(r) == '"a"'


async def test_console_records_and_clears(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        out = text(await call(client, "browser_console", {"page_id": pid, "clear": True}))
        assert "[log] hello from page" in out
        assert "[error] boom" in out
        out = text(await call(client, "browser_console", {"page_id": pid}))
        assert out.startswith("0 console entries")


async def test_console_records_uncaught_page_errors(connect, site):
    async with connect("browser") as client:
        res = await open_page(client, site + "/throws")
        assert any("uncaught kaboom" in e for e in res["new_console_errors"])
        out = text(await call(client, "browser_console", {"page_id": res["page_id"]}))
        assert "[pageerror] uncaught kaboom" in out


async def test_network_failed_only_and_all(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        await call(client, "browser_wait", {"page_id": pid, "load_state": "networkidle"})
        failed = (await call(client, "browser_network", {"page_id": pid})).structured_content
        urls = {(r["url"], r["status"]) for r in failed["requests"]}
        assert (site + "/missing", 404) in urls
        assert all(r["url"] != site + "/" for r in failed["requests"])
        everything = (await call(client, "browser_network", {"page_id": pid, "failed_only": False})).structured_content
        main = next(r for r in everything["requests"] if r["url"] == site + "/")
        assert main["status"] == 200
        assert main["type"] == "document"
        assert main["duration_ms"] is not None


async def test_eval_returns_json_and_reports_script_errors(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        r = await call(client, "browser_eval", {"page_id": pid, "script": "() => ({a: 1, list: [1, 2]})"})
        assert '"list": [' in text(r)
        r = await call(client, "browser_eval", {"page_id": pid, "script": "Promise.resolve(41 + 1)"})
        assert text(r) == "42"
        r = await client.call_tool("browser_eval", {"page_id": pid, "script": "notDefined.x"})
        assert r.is_error
        assert "notDefined" in text(r)


async def test_screenshot_of_page_returns_image_and_saves_file(connect, site, workspace):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        r = await call(client, "browser_screenshot", {"page_id": pid, "name": "home page"})
    images = [b for b in r.content if isinstance(b, types.ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert base64.b64decode(images[0].data).startswith(b"\x89PNG")
    line = text(r)
    rel = line.split(" saved to ")[1].split(" (")[0]
    assert rel.startswith(".dev-link/artifacts/") and rel.endswith("home-page.png")
    saved = workspace / rel
    assert saved.read_bytes().startswith(b"\x89PNG")
    assert (workspace / ".dev-link" / ".gitignore").read_text() == "*\n"


async def test_screenshot_of_url_and_selector(connect, site):
    async with connect("browser") as client:
        r = await client.call_tool("browser_screenshot", {"url": site + "/next", "selector": "#next"})
        assert not r.is_error, text(r)
        assert any(isinstance(b, types.ImageContent) for b in r.content)
        assert site + "/next" in text(r)
        listed = (await call(client, "browser_list_pages")).structured_content
        assert listed["pages"] == []


async def test_screenshot_argument_errors(connect, site):
    async with connect("browser") as client:
        r = await client.call_tool("browser_screenshot", {})
        assert r.is_error and "exactly one" in text(r)
        r = await client.call_tool("browser_screenshot", {"page_id": "p1", "url": site})
        assert r.is_error and "exactly one" in text(r)
        r = await client.call_tool("browser_screenshot", {"page_id": "p99"})
        assert r.is_error and "no open page" in text(r)
        r = await client.call_tool("browser_screenshot", {"url": "file:///etc/passwd"})
        assert r.is_error and "file://" in text(r)


async def test_set_viewport_and_device(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/", width=800, height=600))["page_id"]
        r = await call(client, "browser_eval", {"page_id": pid, "script": "window.innerWidth"})
        assert text(r) == "800"
        await call(client, "browser_set_viewport", {"page_id": pid, "width": 375, "height": 700})
        r = await call(client, "browser_eval", {"page_id": pid, "script": "[innerWidth, innerHeight]"})
        assert text(r).split() == ["[", "375,", "700", "]"]
        phone = await open_page(client, site + "/", device="iPhone 13")
        r = await call(client, "browser_eval", {"page_id": phone["page_id"], "script": "navigator.userAgent"})
        assert "iPhone" in text(r)
        r = await client.call_tool("browser_open", {"url": site, "device": "iPhone 1300"})
        assert r.is_error and "unknown device" in text(r) and "iPhone" in text(r)
        r = await client.call_tool("browser_set_viewport", {"page_id": pid, "width": 10, "height": 700})
        assert r.is_error


async def test_navigate_list_and_close(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        res = (await call(client, "browser_navigate", {"page_id": pid, "url": site + "/next"})).structured_content
        assert res["title"] == "Next" and res["status"] == 200
        listed = (await call(client, "browser_list_pages")).structured_content["pages"]
        assert [(p["page_id"], p["title"]) for p in listed] == [(pid, "Next")]
        assert listed[0]["console_errors"] >= 1
        assert "closed" in text(await call(client, "browser_close", {"page_id": pid}))
        r = await client.call_tool("browser_snapshot", {"page_id": pid})
        assert r.is_error and "no open page" in text(r)
        assert (await call(client, "browser_list_pages")).structured_content["pages"] == []


async def test_popup_is_tracked_as_page(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        await call(client, "browser_click", {"page_id": pid, "selector": "#popup"})
        deadline = time.monotonic() + 5
        pages = []
        while time.monotonic() < deadline:
            pages = (await call(client, "browser_list_pages")).structured_content["pages"]
            if len(pages) == 2:
                break
            await asyncio.sleep(0.1)
        assert len(pages) == 2
        popup = next(p["page_id"] for p in pages if p["page_id"] != pid)
        await call(client, "browser_wait", {"page_id": popup, "selector": "#next"})
        await call(client, "browser_close", {"page_id": pid})
        assert (await call(client, "browser_list_pages")).structured_content["pages"] == []


async def test_wait_conditions_and_timeouts(connect, site, rt):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/late"))["page_id"]
        res = await call(client, "browser_wait", {"page_id": pid, "selector": "#late"})
        assert res.structured_content["waited_s"] < 10
        r = await client.call_tool("browser_wait", {"page_id": pid})
        assert r.is_error and "exactly one" in text(r)
        r = await client.call_tool("browser_wait", {"page_id": pid, "selector": "#late", "text": "Arrived"})
        assert r.is_error and "exactly one" in text(r)
        r = await client.call_tool("browser_wait", {"page_id": pid, "selector": "#never", "timeout_s": 0.3})
        assert r.is_error and "waiting for selector" in text(r)
        r = await client.call_tool("browser_wait", {"page_id": pid, "load_state": "bogus"})
        assert r.is_error
        # A huge timeout is capped by the server limit.
        rt.config.limits.max_timeout_s = 0.5
        started = time.monotonic()
        r = await client.call_tool("browser_click", {"page_id": pid, "selector": "#never", "timeout_s": 600})
        assert r.is_error and "click on '#never' failed" in text(r)
        assert time.monotonic() - started < 10


async def test_rejects_unsupported_urls(connect, site):
    async with connect("browser") as client:
        for url in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings", "not a url"):
            r = await client.call_tool("browser_open", {"url": url})
            assert r.is_error, url
        r = await client.call_tool("browser_open", {"url": "file:///etc/passwd"})
        assert "http.server" in text(r)
        pid = (await open_page(client, site + "/"))["page_id"]
        r = await client.call_tool("browser_navigate", {"page_id": pid, "url": "file:///etc/hostname"})
        assert r.is_error and "file://" in text(r)


NETWORK_OFF = "network access is off on this server"


async def test_network_disabled_refuses_every_http_url(connect, site_server, rt):
    # Chromium runs on the host, so loopback is the host's: its databases, admin UIs and other servers.
    rt.config.sandbox.network = False
    port = site_server.server_address[1]
    urls = [
        "https://example.com/",
        f"http://127.0.0.1:{port}/next?probe=ip",
        f"http://localhost:{port}/next?probe=name",
        f"http://app.localhost:{port}/next?probe=sub",
        f"http://[::1]:{port}/next?probe=v6",
        f"http://127.0.0.1:{port}/redirect-out?probe=redirect",
    ]
    async with connect("browser") as client:
        pid = (await open_page(client, "about:blank"))["page_id"]
        for url in urls:
            for tool, args in [
                ("browser_open", {"url": url}),
                ("browser_navigate", {"page_id": pid, "url": url}),
                ("browser_screenshot", {"url": url}),
            ]:
                r = await client.call_tool(tool, args)
                assert r.is_error, (tool, url)
                assert NETWORK_OFF in text(r), (tool, url)
    assert not [hit for hit in site_server.hits if "probe=" in hit]


async def test_network_disabled_blocks_page_requests_to_loopback(connect, site_server, rt):
    rt.config.sandbox.network = False
    port = site_server.server_address[1]
    script = f"""async () => {{
        const out = {{}};
        for (const host of ["127.0.0.1", "localhost", "app.localhost", "[::1]"]) {{
            try {{ await fetch(`http://${{host}}:{port}/next?probe=fetch-${{host}}`); out[host] = "loaded"; }}
            catch (e) {{ out[host] = "blocked"; }}
        }}
        out.ws = await new Promise((resolve) => {{
            const ws = new WebSocket("ws://127.0.0.1:{port}/?probe=ws");
            ws.onopen = () => resolve("open");
            ws.onclose = () => resolve("closed");
        }});
        out.data = await (await fetch("data:text/plain,inline")).text();
        return out;
    }}"""
    async with connect("browser") as client:
        pid = (await open_page(client, "about:blank"))["page_id"]
        out = text(await call(client, "browser_eval", {"page_id": pid, "script": script}))
        # Blocked requests also log console errors after the JSON result.
        result = json.loads(out.split("\n\nconsole errors", 1)[0])
        assert result == {
            "127.0.0.1": "blocked", "localhost": "blocked", "app.localhost": "blocked", "[::1]": "blocked",
            "ws": "closed", "data": "inline",
        }  # fmt: skip
        # A page navigating itself (through a redirect) is blocked as well.
        await call(
            client,
            "browser_eval",
            {"page_id": pid, "script": f"location.href = 'http://127.0.0.1:{port}/redirect-out?probe=nav'"},
        )
        await asyncio.sleep(1)
    assert not [hit for hit in site_server.hits if "probe=" in hit]


async def test_network_disabled_proxy_blocks_loopback_without_routes(rt, site_server):
    # Routes miss redirect hops, service worker fetches and prefetches; the refusing proxy must
    # catch loopback too, which Chromium would otherwise bypass.
    rt.config.sandbox.network = False
    host = browser._BrowserHost(headless=True, network=False)
    try:
        context = await browser._new_context(rt, host, width=800, height=600, device=None, default_timeout_s=10)
        await context.unroute_all()
        page = await context.new_page()
        with pytest.raises(Exception, match="ERR_PROXY_CONNECTION_FAILED"):
            await page.goto(f"http://127.0.0.1:{site_server.server_address[1]}/next?probe=proxy")
    finally:
        await host.shutdown()
    assert not [hit for hit in site_server.hits if "probe=proxy" in hit]


# A WebRTC offer with a STUN server makes Chromium send a UDP binding request straight away.
STUN_OFFER = """port => {
    const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:127.0.0.1:' + port}]});
    pc.createDataChannel('probe');
    pc.createOffer().then(offer => pc.setLocalDescription(offer));
    window.probePc = pc;
    return 'sent';
}"""


@pytest.fixture
def udp_listener() -> Iterator[socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    yield sock
    sock.close()


async def _udp_received(sock: socket.socket, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            sock.recvfrom(2048)
            return True
        except BlockingIOError:
            await asyncio.sleep(0.05)
    return False


async def test_network_disabled_pages_have_no_webrtc_or_webtransport(connect, rt, udp_listener):
    # WebRTC sends UDP from Chromium itself, past the refusing proxy and the routes.
    rt.config.sandbox.network = False
    port = udp_listener.getsockname()[1]
    script = f"""async () => {{
        const names = ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport'];
        const frame = document.createElement('iframe');
        frame.srcdoc = '<p>child</p>';
        await new Promise(resolve => {{ frame.onload = resolve; document.body.appendChild(frame); }});
        const offer = ({STUN_OFFER});
        let attempt;
        try {{ attempt = offer({port}); }} catch (e) {{ attempt = e.name; }}
        return {{
            page: names.filter(n => n in window),
            frame: names.filter(n => n in frame.contentWindow),
            attempt,
        }};
    }}"""
    async with connect("browser") as client:
        pid = (await open_page(client, "about:blank"))["page_id"]
        out = text(await call(client, "browser_eval", {"page_id": pid, "script": script}))
        result = json.loads(out.split("\n\nconsole errors", 1)[0])
        assert result == {"page": [], "frame": [], "attempt": "ReferenceError"}
        assert not await _udp_received(udp_listener, 2)


async def test_network_disabled_browser_sends_no_webrtc_udp_without_the_init_script(udp_listener):
    # The launch flag must hold on its own: a context without the guard still has RTCPeerConnection.
    host = browser._BrowserHost(headless=True, network=False)
    try:
        chromium = await host.ensure_browser()
        context = await chromium.new_context(proxy={"server": host.sink_proxy()})
        page = await context.new_page()
        assert await page.evaluate(STUN_OFFER, udp_listener.getsockname()[1]) == "sent"
        assert not await _udp_received(udp_listener, 2)
    finally:
        await host.shutdown()


async def test_network_enabled_keeps_webrtc(connect, udp_listener):
    async with connect("browser") as client:
        pid = (await open_page(client, "about:blank"))["page_id"]
        script = f"() => ({STUN_OFFER})({udp_listener.getsockname()[1]})"
        assert text(await call(client, "browser_eval", {"page_id": pid, "script": script})).startswith('"sent"')
        assert await _udp_received(udp_listener, 5)


async def test_network_enabled_loads_loopback(connect, site):
    async with connect("browser") as client:
        res = await open_page(client, site + "/next")
        assert res["status"] == 200 and res["title"] == "Next"


async def test_page_limit(connect, site):
    async with connect("browser") as client:
        for _ in range(browser.BrowserSettings().max_pages):
            await open_page(client, "about:blank")
        r = await client.call_tool("browser_open", {"url": "about:blank"})
        assert r.is_error and "close one first" in text(r)


async def test_unknown_page_and_shutdown(rt, site):
    mcp, (group,) = build_server(rt, "browser")
    try:
        async with Client(mcp) as client:
            r = await client.call_tool("browser_click", {"page_id": "nope", "selector": "a"})
            assert r.is_error and "browser_open first" in text(r)
            await open_page(client, site + "/")
            await group.shutdown()
            assert (await call(client, "browser_list_pages")).structured_content["pages"] == []
            # The browser relaunches on the next use.
            res = await open_page(client, site + "/next")
            assert res["title"] == "Next"
    finally:
        await group.shutdown()


async def test_tools_are_registered_with_annotations(connect):
    async with connect("browser") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    expected = {
        "browser_open", "browser_navigate", "browser_click", "browser_type", "browser_press", "browser_select",
        "browser_wait", "browser_screenshot", "browser_snapshot", "browser_console", "browser_network",
        "browser_eval", "browser_set_viewport", "browser_list_pages", "browser_close",
    }  # fmt: skip
    assert set(tools) == expected
    assert tools["browser_snapshot"].annotations.read_only_hint is True
    assert tools["browser_click"].annotations.open_world_hint is True
    assert tools["browser_eval"].annotations.destructive_hint is True


async def test_eval_times_out_on_a_promise_that_never_settles(connect):
    async with connect("browser") as client:
        pid = (await open_page(client, "about:blank"))["page_id"]
        started = time.monotonic()
        r = await client.call_tool(
            "browser_eval", {"page_id": pid, "script": "new Promise(() => {})", "timeout_s": 0.5}
        )
        assert r.is_error and "did not finish within 0.5s" in text(r)
        assert time.monotonic() - started < 5
        r = await call(client, "browser_eval", {"page_id": pid, "script": "1 + 1"})
        assert text(r) == "2"


async def test_closing_a_popup_keeps_its_opener(connect, site):
    async with connect("browser") as client:
        pid = (await open_page(client, site + "/"))["page_id"]
        await call(client, "browser_click", {"page_id": pid, "selector": "#popup"})
        deadline = time.monotonic() + 5
        pages = []
        while time.monotonic() < deadline and len(pages) < 2:
            pages = (await call(client, "browser_list_pages")).structured_content["pages"]
            await asyncio.sleep(0.1)
        popup = next(p["page_id"] for p in pages if p["page_id"] != pid)
        assert text(await call(client, "browser_close", {"page_id": popup})) == f"closed {popup}"
        listed = (await call(client, "browser_list_pages")).structured_content["pages"]
        assert [p["page_id"] for p in listed] == [pid]


async def test_huge_console_output_is_bounded(connect, site):
    async with connect("browser") as client:
        res = await open_page(client, site + "/chatty")
        assert len(res["new_console_errors"]) == browser.MAX_SUMMARY_ERRORS
        assert res["new_console_errors_total"] == 30
        assert all(len(e) < browser.MAX_EVENT_CHARS + 200 for e in res["new_console_errors"])
        assert "e29" in res["new_console_errors"][-1]


# --- idle shutdown ------------------------------------------------------------------------


async def _wait_until(predicate, timeout_s=10.0, step_s=0.01):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(step_s)


def test_idle_timeout_setting_validation():
    assert browser.BrowserSettings().idle_timeout_s == 300
    assert browser.BrowserSettings.model_validate({"idle_timeout_s": 0}).idle_timeout_s == 0
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        browser.BrowserSettings.model_validate({"idle_timeout_s": -1})


async def test_idle_timeout_closes_browser_and_driver_then_relaunches(rt):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"idle_timeout_s": 0.5}})
    host = group.host
    try:
        async with Client(mcp) as client:
            await open_page(client, "about:blank")
            first = host.browser
            assert first is not None and host.playwright is not None
            await _wait_until(lambda: host.playwright is None)
            assert host.browser is None and not first.is_connected()
            assert host.idle_task is None
            assert (await call(client, "browser_list_pages")).structured_content["pages"] == []
            res = await open_page(client, "about:blank")
            assert res["page_id"] == "p2"
            assert host.browser is not None and host.browser.is_connected()
    finally:
        await group.shutdown()


async def test_stale_page_id_after_idle_close_gets_a_clear_message(rt):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"idle_timeout_s": 0.3}})
    try:
        async with Client(mcp) as client:
            pid = (await open_page(client, "about:blank"))["page_id"]
            await _wait_until(lambda: group.host.playwright is None)
            r = await client.call_tool("browser_snapshot", {"page_id": pid})
            assert r.is_error
            assert text(r).endswith(
                f"page {pid} was closed after 0.3 s without browser use; open it again with browser_open"
            )
            r = await client.call_tool("browser_click", {"page_id": "p99", "selector": "a"})
            assert r.is_error and "no open page" in text(r)
    finally:
        await group.shutdown()


async def test_idle_timer_never_interrupts_a_running_call(rt):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"idle_timeout_s": 0.3}})
    try:
        async with Client(mcp) as client:
            pid = (await open_page(client, "about:blank"))["page_id"]
            browser_before = group.host.browser
            script = "new Promise((resolve) => setTimeout(() => resolve(42), 1500))"
            r = await call(client, "browser_eval", {"page_id": pid, "script": script, "timeout_s": 10})
            assert text(r) == "42"
            assert group.host.browser is browser_before and browser_before.is_connected()
    finally:
        await group.shutdown()


async def test_call_during_idle_close_waits_and_relaunches(rt):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"idle_timeout_s": 0.2}})
    host = group.host
    try:
        async with Client(mcp) as client:
            await open_page(client, "about:blank")
            await _wait_until(lambda: host.idle_closing, step_s=0.001)
            res = await open_page(client, "about:blank")
            assert res["page_id"] == "p2"
            assert host.browser is not None and host.browser.is_connected()
            listed = (await call(client, "browser_list_pages")).structured_content["pages"]
            assert [p["page_id"] for p in listed] == ["p2"]
    finally:
        await group.shutdown()


async def test_idle_timeout_zero_never_closes(rt):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"idle_timeout_s": 0}})
    try:
        async with Client(mcp) as client:
            pid = (await open_page(client, "about:blank"))["page_id"]
            await asyncio.sleep(0.5)
            assert group.host.idle_task is None
            assert group.host.browser is not None and group.host.browser.is_connected()
            await call(client, "browser_snapshot", {"page_id": pid})
    finally:
        await group.shutdown()


async def test_shutdown_cancels_the_idle_timer(rt):
    mcp, (group,) = build_server(rt, "browser")
    async with Client(mcp) as client:
        await open_page(client, "about:blank")
        timer = group.host.idle_task
        assert timer is not None and not timer.done()
        await group.shutdown()
        assert timer.cancelled()
        assert group.host.idle_task is None and group.host.playwright is None


# --- page and context lifetime ------------------------------------------------------------


SELF_CLOSE = "() => { setTimeout(() => window.close()); }"


def _chromium_pages(host) -> int:
    """Pages Chromium really has open, tracked or not."""
    return sum(len(c.pages) for c in host.browser.contexts)


async def test_popups_beyond_the_page_limit_are_closed_and_reported(rt, site):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"max_pages": 2, "idle_timeout_s": 0}})
    host = group.host
    try:
        async with Client(mcp) as client:
            pid = (await open_page(client, site + "/"))["page_id"]
            script = "() => { for (let i = 0; i < 5; i++) window.open('about:blank'); }"
            await call(client, "browser_eval", {"page_id": pid, "script": script})
            await _wait_until(lambda: _chromium_pages(host) == 2 and len(host.pages) == 2)
            await asyncio.sleep(0.5)
            assert len(host.pages) == 2
            assert _chromium_pages(host) == 2
            out = text(await call(client, "browser_console", {"page_id": pid}))
            assert out.count("2 pages are already open") == 4
    finally:
        await group.shutdown()


async def test_page_that_closes_itself_frees_its_context(rt):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"idle_timeout_s": 0}})
    host = group.host
    try:
        async with Client(mcp) as client:
            pid = (await open_page(client, "about:blank"))["page_id"]
            await open_page(client, "about:blank")
            assert len(host.browser.contexts) == 2
            # A popup keeps the shared context alive after its opener closes itself.
            await call(client, "browser_eval", {"page_id": pid, "script": "() => { window.open('about:blank'); }"})
            await _wait_until(lambda: len(host.pages) == 3)
            await call(client, "browser_eval", {"page_id": pid, "script": SELF_CLOSE})
            await _wait_until(lambda: len(host.pages) == 2)
            assert len(host.browser.contexts) == 2
            popup = next(s for s in host.pages.values() if s.popup)
            await popup.page.evaluate(SELF_CLOSE)
            await _wait_until(lambda: len(host.browser.contexts) == 1)
            assert len(host.pages) == 1
    finally:
        await group.shutdown()


async def test_closing_the_last_popup_of_a_closed_opener_frees_the_context(rt):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"idle_timeout_s": 0}})
    host = group.host
    try:
        async with Client(mcp) as client:
            pid = (await open_page(client, "about:blank"))["page_id"]
            await call(client, "browser_eval", {"page_id": pid, "script": "() => { window.open('about:blank'); }"})
            await _wait_until(lambda: len(host.pages) == 2)
            await call(client, "browser_eval", {"page_id": pid, "script": SELF_CLOSE})
            await _wait_until(lambda: len(host.pages) == 1)
            popup = next(iter(host.pages))
            await call(client, "browser_close", {"page_id": popup})
            await _wait_until(lambda: len(host.browser.contexts) == 0)
    finally:
        await group.shutdown()


async def test_cancelled_open_frees_its_page_and_slot(rt, site):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"max_pages": 1, "idle_timeout_s": 0}})
    host = group.host
    try:
        opening = asyncio.create_task(mcp.call_tool("browser_open", {"url": site + "/slow", "timeout_s": 30}))
        await _wait_until(lambda: len(host.pages) == 1)
        await asyncio.sleep(0.2)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert host.pages == {}
        await _wait_until(lambda: len(host.browser.contexts) == 0)
        result = await mcp.call_tool("browser_open", {"url": "about:blank"})
        assert not result.is_error, text(result)
    finally:
        await group.shutdown()


async def test_concurrent_opens_respect_the_page_limit(rt):
    mcp, (group,) = build_server(rt, "browser", settings={"browser": {"max_pages": 2, "idle_timeout_s": 0}})
    host = group.host
    try:
        results = await asyncio.gather(
            *(mcp.call_tool("browser_open", {"url": "about:blank"}) for _ in range(5)), return_exceptions=True
        )
        opened = [r for r in results if isinstance(r, types.CallToolResult) and not r.is_error]
        refused = [r for r in results if not isinstance(r, types.CallToolResult) or r.is_error]
        assert len(opened) == 2
        assert len(refused) == 3
        assert all("close one first" in str(r) for r in refused)
        assert len(host.pages) == 2
        assert _chromium_pages(host) == 2
    finally:
        await group.shutdown()
