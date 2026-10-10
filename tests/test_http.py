import gzip
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import text

from dev_link_mcp.tools.http import html_to_text

PAGE = """<!doctype html><html><head><title> Demo  Page </title>
<style>body { color: red }</style><script>var secret = "SCRIPT_TEXT";</script></head>
<body><nav><a href="/home">NAV_LINK</a></nav>
<h1>Main heading</h1>
<p>First   paragraph with <a href="/docs/intro">a relative link</a> and <code>inline()</code>.</p>
<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>
<pre>def f():
    return 1</pre>
<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>
<a href="javascript:void(0)">js link</a> <a href="#top"></a>
<noscript>NOSCRIPT_TEXT</noscript>
</body></html>"""

BINARY = bytes(range(256)) * 40


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="text/plain", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        path = self.path
        if path == "/echo":
            payload = {
                "method": self.command,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": data.decode(),
            }
            return self._send(200, json.dumps(payload).encode(), "application/json")
        if path == "/html":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/plain":
            return self._send(200, b"just text", "text/plain")
        if path == "/binary":
            return self._send(200, BINARY, "application/octet-stream")
        if path == "/files/report.csv":
            return self._send(200, b"a,b\n1,2\n", "text/csv")
        if path == "/big":
            return self._send(200, b"x" * 50_000, "text/plain")
        if path == "/gzip":
            body = gzip.compress(b"y" * 100_000)
            return self._send(200, body, "text/plain", {"Content-Encoding": "gzip"})
        if path == "/missing":
            return self._send(404, b"<html><body><p>Not here</p></body></html>", "text/html")
        if path == "/error":
            return self._send(500, b'{"error": "boom"}', "application/json")
        if path == "/redirect":
            return self._send(302, b"", extra={"Location": "/plain"})
        if path == "/redirect-external":
            return self._send(302, b"", extra={"Location": "http://203.0.113.9/x"})
        if path == "/loop":
            return self._send(302, b"", extra={"Location": "/loop"})
        if path == "/drip":
            # Each byte arrives well inside httpx's per-read timeout; only a total deadline stops it.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "30")
            self.end_headers()
            for _ in range(30):
                self.wfile.write(b"d")
                self.wfile.flush()
                time.sleep(0.1)
            return None
        if path == "/slow":
            time.sleep(1.5)
            return self._send(200, b"late")
        return self._send(404, b"no route")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _route


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Clients that abort mid-body (timeouts, max_bytes) are expected here.
        pass


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = _QuietServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


# http_request


async def test_get_json_is_pretty_printed(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/echo"})
    assert not r.is_error
    out = text(r)
    assert out.startswith(f"GET {server}/echo -> 200 OK")
    assert '\n  "method": "GET",' in out
    assert "content-type: application/json" in out


async def test_post_json_body_and_custom_headers(connect, server):
    async with connect("http") as client:
        r = await client.call_tool(
            "http_request",
            {"url": f"{server}/echo", "method": "POST", "json": {"a": [1, 2]}, "headers": {"X-Trace": "abc"}},
        )
    assert not r.is_error
    payload = json.loads(text(r).split("body:\n", 1)[1])
    assert payload["method"] == "POST"
    assert json.loads(payload["body"]) == {"a": [1, 2]}
    assert payload["headers"]["content-type"] == "application/json"
    assert payload["headers"]["x-trace"] == "abc"


async def test_raw_body_with_put(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/echo", "method": "PUT", "body": "raw=1"})
    payload = json.loads(text(r).split("body:\n", 1)[1])
    assert payload["method"] == "PUT"
    assert payload["body"] == "raw=1"


async def test_credentials_redacted_in_echo_but_sent(connect, server):
    headers = {"Authorization": "Bearer SECRET-TOKEN", "Cookie": "sid=SECRET-COOKIE", "X-Plain": "visible"}
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/echo", "headers": headers})
    out = text(r)
    request_echo = out.split("response headers:", 1)[0]
    assert "authorization: [redacted]" in request_echo
    assert "cookie: [redacted]" in request_echo
    assert "x-plain: visible" in request_echo
    assert "SECRET" not in request_echo
    # The server did receive the real values.
    payload = json.loads(out.split("body:\n", 1)[1])
    assert payload["headers"]["authorization"] == "Bearer SECRET-TOKEN"


async def test_error_status_is_returned_not_raised(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/error"})
    assert not r.is_error
    assert "-> 500" in text(r)
    assert '"error": "boom"' in text(r)


async def test_head_has_no_body(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/plain", "method": "HEAD"})
    assert not r.is_error
    assert "(HEAD request, no body)" in text(r)


async def test_redirects_followed_and_reported(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/redirect"})
        no_follow = await client.call_tool("http_request", {"url": f"{server}/redirect", "follow_redirects": False})
    assert "redirects: 1, final url: " in text(r)
    assert "just text" in text(r)
    assert "-> 302" in text(no_follow)
    assert "location: /plain" in text(no_follow)


async def test_redirect_loop_is_an_error(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/loop"})
    assert r.is_error
    assert "more than 10 redirects" in text(r)


async def test_binary_body_summarized(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/binary"})
    assert f"binary body, {len(BINARY)} bytes" in text(r)
    assert "download_file" in text(r)


async def test_long_body_clipped_with_hint(connect, server, rt):
    rt.config.limits.max_output_bytes = 1000
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/big"})
    out = text(r)
    assert "characters elided" in out
    assert "body truncated" in out
    assert len(out) < 5000


async def test_read_limit_stops_decompressed_body(connect, server, rt):
    rt.config.limits.max_read_bytes = 10_000
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/gzip"})
    out = text(r)
    assert "body truncated: read limit 10000 bytes" in out
    assert max(len(run) for run in re.findall("y+", out)) == 10_000


async def test_body_and_json_conflict(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/echo", "body": "x", "json": {"a": 1}})
    assert r.is_error
    assert "either body or json" in text(r)


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/x", "file:///etc/passwd", "not a url", "http://"])
async def test_bad_urls_rejected(connect, url):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": url})
    assert r.is_error


async def test_invalid_method_rejected(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/echo", "method": "TRACE"})
    assert r.is_error


async def test_timeout(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/slow", "timeout_s": 0.3})
    assert r.is_error
    assert "timed out after 0.3s" in text(r)


async def test_timeout_is_total_not_per_read(connect, server, workspace):
    async with connect("http") as client:
        start = time.monotonic()
        r = await client.call_tool("http_request", {"url": f"{server}/drip", "timeout_s": 0.5})
        f = await client.call_tool("fetch_url", {"url": f"{server}/drip", "timeout_s": 0.5})
        d = await client.call_tool("download_file", {"url": f"{server}/drip", "path": "d.txt", "timeout_s": 0.5})
        elapsed = time.monotonic() - start
    for result in (r, f, d):
        assert result.is_error
        assert "timed out after 0.5s" in text(result)
    assert elapsed < 2.5
    assert list(workspace.iterdir()) == []


async def test_non_ascii_header_is_a_clear_error(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/echo", "headers": {"X-Name": "café"}})
    assert r.is_error
    assert "must be ASCII" in text(r)


@pytest.mark.parametrize("timeout", [0, -1, 999_999])
async def test_timeout_bounds(connect, server, timeout):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/plain", "timeout_s": timeout})
    assert r.is_error
    assert "timeout_s must be" in text(r)


async def test_connection_refused(connect):
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": "http://127.0.0.1:1/"})
    assert r.is_error
    assert "request failed: ConnectError" in text(r)
    assert "network namespace" not in text(r)


# network off


async def test_network_off_blocks_external_hosts(connect, rt):
    rt.config.sandbox.network = False
    async with connect("http") as client:
        for tool, args in [
            ("http_request", {"url": "http://example.com/"}),
            ("fetch_url", {"url": "https://203.0.113.5/"}),
            ("download_file", {"url": "http://10.0.0.1/a", "path": "a"}),
        ]:
            r = await client.call_tool(tool, args)
            assert r.is_error, tool
            assert "only loopback URLs are allowed" in text(r)


async def test_network_off_allows_loopback(connect, rt, server):
    rt.config.sandbox.network = False
    port = server.rsplit(":", 1)[1]
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/plain"})
        named = await client.call_tool("http_request", {"url": f"http://localhost:{port}/plain"})
    assert not r.is_error
    assert "just text" in text(r)
    assert not named.is_error


def _fake_resolver(monkeypatch, answers: dict[str, list[str]]) -> None:
    """Make getaddrinfo return the given addresses for the given names; other names resolve normally."""
    import socket

    real = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host in answers:
            family = {4: socket.AF_INET, 6: socket.AF_INET6}
            return [
                (family[ip_version(a)], socket.SOCK_STREAM, 6, "", (a, port) if ":" not in a else (a, port, 0, 0))
                for a in answers[host]
            ]
        return real(host, port, *args, **kwargs)

    def ip_version(address: str) -> int:
        return 6 if ":" in address else 4

    monkeypatch.setattr(socket, "getaddrinfo", fake)


async def test_network_off_localhost_subdomain_resolving_public_is_blocked(connect, rt, monkeypatch):
    # Without nss-myhostname a *.localhost name can go to DNS and come back with any address.
    rt.config.sandbox.network = False
    _fake_resolver(monkeypatch, {"evil.localhost": ["169.254.169.254"], "mixed.localhost": ["127.0.0.1", "10.0.0.1"]})
    async with connect("http") as client:
        evil = await client.call_tool("http_request", {"url": "http://evil.localhost/latest/meta-data/"})
        mixed = await client.call_tool("fetch_url", {"url": "http://mixed.localhost/"})
    assert evil.is_error
    assert "169.254.169.254" in text(evil)
    assert "not only loopback" in text(evil)
    assert mixed.is_error
    assert "not only loopback" in text(mixed)


async def test_network_off_localhost_subdomain_pinned_to_loopback(connect, rt, server, monkeypatch):
    # ::1 is tried first and refused (the test server is IPv4 only), then 127.0.0.1 answers.
    rt.config.sandbox.network = False
    _fake_resolver(monkeypatch, {"app.localhost": ["::1", "127.0.0.1"]})
    port = server.rsplit(":", 1)[1]
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"http://app.localhost:{port}/echo"})
    assert not r.is_error, text(r)
    assert f'"host": "app.localhost:{port}"' in text(r)


async def test_network_off_refused_loopback_explains_namespaces(connect, rt):
    # Sandboxed servers live in their own network namespace when the network is off, so a refused
    # loopback connection usually means the agent is trying to reach one of them.
    rt.config.sandbox.network = False
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": "http://127.0.0.1:1/"})
        d = await client.call_tool("download_file", {"url": "http://127.0.0.1:1/a", "path": "a"})
    for result in (r, d):
        assert result.is_error
        assert "ConnectError" in text(result)
        assert "own network namespace" in text(result)


async def test_network_off_blocks_redirect_to_external(connect, rt, server, workspace):
    rt.config.sandbox.network = False
    async with connect("http") as client:
        r = await client.call_tool("http_request", {"url": f"{server}/redirect-external"})
        d = await client.call_tool("download_file", {"url": f"{server}/redirect-external", "path": "out.bin"})
    assert r.is_error
    assert "203.0.113.9" in text(r)
    assert d.is_error
    assert list(workspace.iterdir()) == []


# fetch_url


async def test_fetch_url_converts_html(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("fetch_url", {"url": f"{server}/html"})
    assert not r.is_error
    out = text(r)
    assert out.startswith("# Demo Page\nURL: ")
    assert "# Main heading" in out
    assert f"[a relative link]({server}/docs/intro)" in out
    assert "First paragraph with" in out
    assert "`inline()`" in out
    assert "- one" in out
    assert "- one\n- two\n  - nested\n" in out
    assert "```\ndef f():\n    return 1\n```" in out
    assert "| a | b" in out
    for dropped in ("SCRIPT_TEXT", "color: red", "NAV_LINK", "NOSCRIPT_TEXT", "javascript:", "#top"):
        assert dropped not in out


async def test_fetch_url_plain_text(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("fetch_url", {"url": f"{server}/plain"})
    assert text(r) == f"URL: {server}/plain\n\njust text"


async def test_fetch_url_http_error(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("fetch_url", {"url": f"{server}/missing"})
    assert r.is_error
    assert "HTTP 404" in text(r)
    assert "Not here" in text(r)


async def test_fetch_url_binary_refused(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("fetch_url", {"url": f"{server}/binary"})
    assert r.is_error
    assert "download_file" in text(r)


async def test_fetch_url_clipped(connect, server, rt):
    rt.config.limits.max_output_bytes = 500
    async with connect("http") as client:
        r = await client.call_tool("fetch_url", {"url": f"{server}/big"})
    assert "content truncated" in text(r)


def test_html_to_text_drops_empty_links_and_keeps_image_links():
    html = '<a href="/a"></a><a href="/b"> <img src="i.png"> </a><a href="/c"><img alt="logo"></a> [](x'
    _, body = html_to_text(html, "http://h/")
    assert "/a)" not in body
    assert "/b)" not in body
    assert "[ [image: logo] ](http://h/c)" in body
    # Literal bracket text from the page is kept as is.
    assert body.endswith("[](x")


def test_html_to_text_is_linear_on_bracket_heavy_text():
    # A quadratic cleanup regex took minutes on input like this.
    start = time.monotonic()
    html_to_text("<p>" + "[](" * 200_000 + "</p>", "http://h/")
    assert time.monotonic() - start < 5


def test_html_to_text_handles_malformed_markup():
    title, body = html_to_text("<p>open <b>bold <a href='x'>link</a></p></div><li>stray<script>no", "http://h/base/")
    assert title == ""
    assert "open bold [link](http://h/base/x)" in body
    assert body.endswith("- stray")


# download_file


async def test_download_to_file(connect, server, workspace):
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/binary", "path": "sub/dir/blob.bin"})
    assert not r.is_error
    saved = workspace / "sub/dir/blob.bin"
    assert saved.read_bytes() == BINARY
    out = text(r)
    assert f"saved {len(BINARY)} bytes to sub/dir/blob.bin" in out
    assert hashlib.sha256(BINARY).hexdigest() in out
    assert not [p for p in saved.parent.iterdir() if p.name.startswith(".download-")]


async def test_download_into_directory_uses_url_name(connect, server, workspace):
    (workspace / "data").mkdir()
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/files/report.csv", "path": "data"})
    assert not r.is_error
    assert (workspace / "data/report.csv").read_text() == "a,b\n1,2\n"


async def test_download_overwrites_existing_file(connect, server, workspace):
    (workspace / "f.txt").write_text("old")
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/plain", "path": "f.txt"})
    assert not r.is_error
    assert (workspace / "f.txt").read_text() == "just text"


async def test_download_max_bytes_leaves_nothing(connect, server, workspace):
    (workspace / "keep.bin").write_text("original")
    async with connect("http") as client:
        declared = await client.call_tool(
            "download_file", {"url": f"{server}/big", "path": "big.txt", "max_bytes": 100}
        )
        streamed = await client.call_tool(
            "download_file", {"url": f"{server}/gzip", "path": "keep.bin", "max_bytes": 5000}
        )
    assert declared.is_error
    assert "over max_bytes=100" in text(declared)
    assert streamed.is_error
    assert "exceeded max_bytes=5000" in text(streamed)
    assert sorted(p.name for p in workspace.iterdir()) == ["keep.bin"]
    assert (workspace / "keep.bin").read_text() == "original"


async def test_download_http_error_writes_nothing(connect, server, workspace):
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/missing", "path": "x.html"})
    assert r.is_error
    assert "HTTP 404" in text(r)
    assert list(workspace.iterdir()) == []


async def test_download_max_bytes_must_be_positive(connect, server):
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/plain", "path": "x", "max_bytes": 0})
    assert r.is_error


async def test_download_to_workspace_root_directory(connect, server, workspace):
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/files/report.csv", "path": "."})
    assert not r.is_error
    assert "to report.csv" in text(r)
    assert (workspace / "report.csv").exists()


async def test_download_destination_under_a_file(connect, server, workspace):
    (workspace / "f.txt").write_text("x")
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/plain", "path": "f.txt/out.txt"})
    assert r.is_error
    assert "cannot write to f.txt/out.txt" in text(r)


def test_download_name_fallback():
    import httpx

    from dev_link_mcp.tools.http import _download_name

    assert _download_name(httpx.URL("http://h/")) == "download"
    assert _download_name(httpx.URL("http://h/a/b%20c.tar.gz?x=1")) == "b c.tar.gz"
    assert _download_name(httpx.URL("http://h/a/..")) == "download"


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/dev-link-escape.txt", "link/escape.txt", "outlink"])
@pytest.mark.bwrap
async def test_download_path_escapes_rejected(connect, server, workspace, tmp_path, path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / "link")
    os.symlink(outside / "target.txt", workspace / "outlink")
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/plain", "path": path})
    assert r.is_error
    assert "outside the workspace" in text(r)
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "escape.txt").exists()


async def test_tools_registered_with_open_world_annotations(connect):
    async with connect("http") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"http_request", "fetch_url", "download_file"}
    for tool in tools.values():
        assert tool.annotations.open_world_hint is True
    assert tools["fetch_url"].annotations.read_only_hint is True


@pytest.mark.parametrize("before", ["make_dirs", "write_file"])
async def test_download_refuses_a_directory_swapped_for_a_symlink(
    connect, server, workspace, rt, tmp_path, monkeypatch, before
):
    from test_symlink_race import swap_before, swap_dir

    import dev_link_mcp.workspace as workspace_module

    rt.workspace.confined = True
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "a").mkdir()
    swap_before(monkeypatch, workspace_module.Workspace, before, swap_dir(workspace, outside))
    async with connect("http") as client:
        r = await client.call_tool("download_file", {"url": f"{server}/binary", "path": "a/authorized_keys"})
    assert r.is_error
    assert "replaced by a symlink" in text(r)
    assert list(outside.iterdir()) == []
