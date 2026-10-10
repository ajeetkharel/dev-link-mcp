"""HTTP client tools: raw requests, readable page fetches, and downloads into the workspace.

Requests are made by the server process with httpx, not inside the sandbox. When the
sandbox network is off, every request is refused: the server's loopback is the host's, not the sandbox's.
"""

import asyncio
import hashlib
import json as jsonlib
import re
import tempfile
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urljoin

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import OPEN_WORLD, OPEN_WORLD_READ, clip

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Request headers whose values are credentials; echoed back as [redacted].
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-api-key", "x-auth-token"})
_TEXT_TYPE_MARKERS = ("json", "xml", "javascript", "x-www-form-urlencoded", "yaml", "csv", "graphql")


def _failure(action: str, exc: httpx.HTTPError) -> str:
    return f"{action}: {type(exc).__name__}: {exc or 'no details'}"


def _check_url(rt: Runtime, url: httpx.URL) -> None:
    """Reject every URL when the sandbox network is off, and non-HTTP schemes or hostless URLs otherwise."""
    if not rt.config.sandbox.network:
        raise ToolError(
            "network access is off on this server, so HTTP requests are refused; ask the user to restart"
            " the server with network access"
        )
    if url.scheme not in ("http", "https"):
        raise ToolError(f"only http and https URLs are supported, got {url.scheme or 'no'} scheme: {url}")
    if not url.host:
        raise ToolError(f"URL has no host: {url}")


def _parse_url(rt: Runtime, url: str) -> httpx.URL:
    try:
        parsed = httpx.URL(url.strip())
    except httpx.InvalidURL as exc:
        raise ToolError(f"invalid URL {url!r}: {exc}") from exc
    _check_url(rt, parsed)
    return parsed


def _timeout(rt: Runtime, timeout_s: float | None, default_s: float) -> float:
    value = default_s if timeout_s is None else timeout_s
    if value <= 0:
        raise ToolError("timeout_s must be greater than 0")
    if value > rt.config.limits.max_timeout_s:
        raise ToolError(f"timeout_s must be at most {rt.config.limits.max_timeout_s:.0f}")
    return value


def _client(rt: Runtime, timeout_s: float, follow_redirects: bool, max_redirects: int) -> httpx.AsyncClient:
    async def guard(request: httpx.Request) -> None:
        # Runs for every redirect hop too, so a redirect cannot lead somewhere the first check would refuse.
        _check_url(rt, request.url)

    return httpx.AsyncClient(
        timeout=timeout_s,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        event_hooks={"request": [guard]},
    )


@dataclass
class _Fetched:
    response: httpx.Response
    body: bytes
    truncated: bool
    duration_s: float


async def _fetch(
    rt: Runtime,
    method: str,
    url: httpx.URL,
    *,
    timeout_s: float,
    follow_redirects: bool,
    max_redirects: int,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    content: str | None = None,
    json_body: Any = None,
) -> _Fetched:
    """Send one request and read at most `max_bytes` of the decoded body.

    Raises ToolError for timeouts, connection failures, too many redirects, and blocked URLs.
    HTTP error statuses are returned, not raised.
    """
    start = time.monotonic()
    try:
        # httpx timeouts apply per network operation, so a slowly dripping body would never trip them.
        async with (
            asyncio.timeout(timeout_s),
            _client(rt, timeout_s, follow_redirects, max_redirects) as client,
            client.stream(method, url, headers=headers, content=content, json=json_body) as response,
        ):
            buf = bytearray()
            truncated = False
            if method != "HEAD":
                async for chunk in response.aiter_bytes():
                    room = max_bytes - len(buf)
                    if len(chunk) > room:
                        buf += chunk[:room]
                        truncated = True
                        break
                    buf += chunk
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise ToolError(f"request timed out after {timeout_s:g}s: {url}") from exc
    except httpx.TooManyRedirects as exc:
        raise ToolError(f"more than {max_redirects} redirects: {url}") from exc
    except httpx.HTTPError as exc:
        raise ToolError(_failure("request failed", exc)) from exc
    except UnicodeEncodeError as exc:
        raise ToolError(f"header names and values must be ASCII: {exc}") from exc
    return _Fetched(response, bytes(buf), truncated, time.monotonic() - start)


def _is_text(content_type: str, body: bytes) -> bool:
    ctype = content_type.split(";", 1)[0].strip().lower()
    if ctype.startswith("text/") or any(marker in ctype for marker in _TEXT_TYPE_MARKERS):
        return True
    if ctype:
        return False
    return b"\x00" not in body[:8192]


def _decode(response: httpx.Response, body: bytes) -> str:
    try:
        return body.decode(response.encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _render_body(response: httpx.Response, body: bytes) -> str:
    """Body as agent-facing text: JSON pretty-printed, binary summarized instead of dumped."""
    content_type = response.headers.get("content-type", "")
    if not body:
        return "(empty body)"
    if not _is_text(content_type, body):
        return f"(binary body, {len(body)} bytes of {content_type or 'unknown type'}; use download_file to save it)"
    text = _decode(response, body)
    if "json" in content_type.lower():
        try:
            return jsonlib.dumps(jsonlib.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return text


def _redacted(headers: httpx.Headers) -> list[str]:
    return [
        f"  {name}: {'[redacted]' if name.lower() in _SECRET_HEADERS else value}"
        for name, value in headers.multi_items()
    ]


_INDENT = "\x01"


class _Readable(HTMLParser):
    """Turns HTML into markdown-ish text: headings, lists, links, code blocks, table rows.

    Drops scripts, styles, navigation and other non-content elements.
    """

    _SKIP = frozenset({"script", "style", "nav", "noscript", "template", "svg", "iframe", "head", "button", "select"})
    _BLOCK = frozenset({
        "p", "div", "section", "article", "main", "header", "footer", "aside", "form", "table", "tr",
        "dl", "dt", "dd", "blockquote", "figure", "figcaption", "details", "summary", "br", "hr",
    })  # fmt: skip

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.out: list[str] = []
        self._skip = 0
        self._in_title = False
        self._pre = 0
        # Per open <a>: (index of its "[" in `out`, href), or None when the link is not rendered.
        self._links: list[tuple[int, str] | None] = []
        self._lists = 0

    def _emit(self, text: str) -> None:
        if not self._skip:
            self.out.append(text)

    def _href(self, raw: str | None) -> str | None:
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "data:")):
            return None
        return urljoin(self.base_url, raw)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "title":
            self._in_title = True
            return
        if tag in self._SKIP:
            self._skip += 1
            return
        if tag in ("ul", "ol"):
            self._lists += 1
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._emit(f"\n\n{'#' * int(tag[1])} ")
        elif tag == "li":
            # Indent marker survives the per-line strip in text(); spaces would not.
            self._emit("\n" + _INDENT * max(self._lists - 1, 0) + "- ")
        elif tag == "pre":
            self._pre += 1
            self._emit("\n\n```\n")
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag in ("td", "th"):
            self._emit(" | ")
        elif tag == "a":
            href = self._href(attr.get("href"))
            if href and not self._skip:
                self._links.append((len(self.out), href))
                self.out.append("[")
            else:
                self._links.append(None)
        elif tag == "img" and attr.get("alt"):
            self._emit(f" [image: {attr['alt']}] ")
        elif tag in self._BLOCK:
            self._emit("\n\n" if tag in ("p", "blockquote", "table", "hr") else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in self._SKIP:
            self._skip = max(self._skip - 1, 0)
            return
        if tag in ("ul", "ol"):
            self._lists = max(self._lists - 1, 0)
            self._emit("\n")
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._emit("\n\n")
        elif tag == "pre" and self._pre:
            self._pre -= 1
            self._emit("\n```\n\n")
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag == "a" and self._links:
            link = self._links.pop()
            if link is None:
                return
            start, href = link
            if "".join(self.out[start + 1 :]).strip():
                self._emit(f"]({href})")
            else:
                # Icon-only or empty anchors would leave "[](url)" noise.
                del self.out[start:]
        elif tag in self._BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        self._emit(data if self._pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        lines: list[str] = []
        in_fence = False
        for line in "".join(self.out).split("\n"):
            if line.strip() == "```":
                in_fence = not in_fence
                lines.append("```")
                continue
            lines.append(line.rstrip() if in_fence else line.strip().replace(_INDENT, "  "))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str, base_url: str) -> tuple[str, str]:
    """(title, readable markdown-ish text) for an HTML document; relative links resolve against base_url."""
    parser = _Readable(base_url)
    parser.feed(html)
    parser.close()
    return re.sub(r"\s+", " ", parser.title).strip(), parser.text()


def _reason(exc: OSError | ToolError) -> str:
    return (exc.strerror if isinstance(exc, OSError) else None) or str(exc)


def _download_name(url: httpx.URL) -> str:
    name = Path(unquote(url.path)).name
    return name if name not in ("", ".", "..") else "download"


class HttpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    default_timeout_s: float = Field(
        default=30.0,
        gt=0,
        description="Total timeout in seconds for http_request, fetch_url and download_file when the agent does "
        "not set one. Must not exceed `limits.max_timeout_s`.",
    )
    download_max_bytes: int = Field(
        default=100_000_000, gt=0, description="Largest download_file result in bytes when the agent does not set one."
    )
    max_redirects: int = Field(default=10, ge=0, description="Most redirects a request follows before it fails.")


def _register(mcp: MCPServer, rt: Runtime, settings: HttpSettings) -> None:
    default_timeout_s = settings.default_timeout_s
    max_redirects = settings.max_redirects
    timeout_help = f"Total timeout in seconds. Default {default_timeout_s:g}, max {rt.config.limits.max_timeout_s:.0f}."

    @mcp.tool(annotations=OPEN_WORLD)
    async def http_request(
        url: Annotated[str, Field(description="Absolute http:// or https:// URL.")],
        method: Annotated[Method, Field(description="HTTP method.")] = "GET",
        headers: Annotated[dict[str, str] | None, Field(description="Request headers.")] = None,
        body: Annotated[str | None, Field(description="Raw request body. Do not combine with json.")] = None,
        json: Annotated[Any, Field(description="Value sent as a JSON body with Content-Type application/json.")] = None,
        timeout_s: Annotated[float | None, Field(description=timeout_help)] = None,
        follow_redirects: Annotated[bool, Field(description=f"Follow 3xx redirects (up to {max_redirects}).")] = True,
    ) -> str:
        """Send an HTTP request from the server process, not the sandbox, and return the status,
        request headers (credentials redacted), response headers and body. JSON is pretty-printed,
        binary bodies are summarized and long bodies are clipped; use download_file for the whole
        body. 4xx/5xx responses are normal results."""
        if body is not None and json is not None:
            raise ToolError("pass either body or json, not both")
        parsed = _parse_url(rt, url)
        timeout = _timeout(rt, timeout_s, default_timeout_s)
        fetched = await _fetch(
            rt,
            method,
            parsed,
            timeout_s=timeout,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
            max_bytes=rt.config.limits.max_read_bytes,
            headers=headers,
            content=body,
            json_body=json,
        )
        resp = fetched.response
        lines = [f"{method} {parsed} -> {resp.status_code} {resp.reason_phrase} in {fetched.duration_s:.2f}s"]
        if resp.history:
            lines.append(f"redirects: {len(resp.history)}, final url: {resp.url}")
        lines += ["", "request headers:", *_redacted(resp.request.headers), "", "response headers:"]
        lines += [f"  {name}: {value}" for name, value in resp.headers.multi_items()]
        # Pretty-printing a body near the read limit is CPU work; keep it off the event loop.
        rendered = (
            await asyncio.to_thread(_render_body, resp, fetched.body) if method != "HEAD" else "(HEAD request, no body)"
        )
        limit = rt.config.limits.max_output_bytes
        lines += ["", "body:", clip(rendered, limit)]
        if fetched.truncated or len(rendered) > limit:
            lines.append(
                f"\n[body truncated: read limit {rt.config.limits.max_read_bytes} bytes, output limit {limit}"
                " characters; use download_file to save the whole body and read_file or grep to inspect it]"
            )
        return "\n".join(lines)

    @mcp.tool(annotations=OPEN_WORLD_READ)
    async def fetch_url(
        url: Annotated[str, Field(description="Absolute http:// or https:// URL of a page or document.")],
        timeout_s: Annotated[float | None, Field(description=timeout_help)] = None,
    ) -> str:
        """GET a URL and return its readable content: HTML as markdown-style text without scripts,
        styles and navigation, JSON pretty-printed, other text as is. Long pages are clipped. Fails
        on 4xx/5xx and on binary content (use download_file for that)."""
        parsed = _parse_url(rt, url)
        fetched = await _fetch(
            rt,
            "GET",
            parsed,
            timeout_s=_timeout(rt, timeout_s, default_timeout_s),
            follow_redirects=True,
            max_redirects=max_redirects,
            max_bytes=rt.config.limits.max_read_bytes,
            headers={"Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8"},
        )
        resp = fetched.response
        content_type = resp.headers.get("content-type", "")
        if not _is_text(content_type, fetched.body):
            raise ToolError(
                f"{resp.url} returned binary content ({content_type or 'unknown type'}); use download_file instead"
            )
        if "html" in content_type.lower() or (not content_type and b"<html" in fetched.body[:4096].lower()):
            # Parsing a multi-megabyte page takes seconds; keep it off the event loop.
            title, content = await asyncio.to_thread(html_to_text, _decode(resp, fetched.body), str(resp.url))
        else:
            title, content = "", await asyncio.to_thread(_render_body, resp, fetched.body)
        if resp.is_error:
            raise ToolError(f"{resp.url} returned HTTP {resp.status_code} {resp.reason_phrase}:\n{content[:1000]}")
        header = [f"# {title}"] if title else []
        header.append(f"URL: {resp.url}")
        limit = rt.config.limits.max_output_bytes
        out = "\n".join(header) + "\n\n" + clip(content or "(no readable text)", limit)
        if fetched.truncated or len(content) > limit:
            out += (
                "\n\n[content truncated; use download_file to save the full document and read_file to page through it]"
            )
        return out

    @mcp.tool(annotations=OPEN_WORLD)
    async def download_file(
        url: Annotated[str, Field(description="Absolute http:// or https:// URL to download.")],
        path: Annotated[
            str,
            Field(description="Destination file in the workspace. An existing directory gets the URL's file name."),
        ],
        max_bytes: Annotated[
            int, Field(description="Abort if the download is larger than this many bytes.", gt=0)
        ] = settings.download_max_bytes,
        timeout_s: Annotated[float | None, Field(description=timeout_help)] = None,
    ) -> str:
        """Download a URL into a workspace file, following redirects, creating parent directories
        and overwriting an existing file. Nothing is written unless the response is 2xx and within
        max_bytes. Returns the saved path, size, content type and sha256."""
        parsed = _parse_url(rt, url)
        timeout = _timeout(rt, timeout_s, default_timeout_s)
        target = rt.workspace.resolve(path)
        if target.is_dir():
            target = rt.workspace.resolve(target / _download_name(parsed))
        if target.is_dir():
            raise ToolError(f"destination is a directory: {rt.workspace.rel(target)}")
        try:
            await asyncio.to_thread(rt.workspace.make_dirs, target.parent)
        except (OSError, ToolError) as exc:
            raise ToolError(f"cannot write to {rt.workspace.rel(target)}: {_reason(exc)}") from exc
        digest = hashlib.sha256()
        size = 0
        # Downloaded into an anonymous temp file outside the workspace, then written into it through
        # write_file, which refuses symlinks; nothing is created in the workspace on failure.
        with tempfile.TemporaryFile() as fh:
            try:
                async with (
                    asyncio.timeout(timeout),
                    _client(rt, timeout, True, max_redirects) as client,
                    client.stream("GET", parsed) as resp,
                ):
                    if not resp.is_success:
                        raise ToolError(f"{resp.url} returned HTTP {resp.status_code} {resp.reason_phrase}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise ToolError(f"download is {declared} bytes, over max_bytes={max_bytes}")
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ToolError(f"download exceeded max_bytes={max_bytes}; nothing was saved")
                        digest.update(chunk)
                        await asyncio.to_thread(fh.write, chunk)
                    final_url = resp.url
                    content_type = resp.headers.get("content-type", "unknown")
            except (httpx.TimeoutException, TimeoutError) as exc:
                raise ToolError(f"download timed out after {timeout:g}s: {parsed}") from exc
            except httpx.TooManyRedirects as exc:
                raise ToolError(f"more than {max_redirects} redirects: {parsed}") from exc
            except httpx.HTTPError as exc:
                raise ToolError(_failure("download failed", exc)) from exc
            fh.seek(0)
            try:
                await asyncio.to_thread(rt.workspace.write_file, target, fh)
            except (OSError, ToolError) as exc:
                raise ToolError(f"cannot write to {rt.workspace.rel(target)}: {_reason(exc)}") from exc
        return (
            f"saved {size} bytes to {rt.workspace.rel(target)}\n"
            f"url: {final_url}\ncontent-type: {content_type}\nsha256: {digest.hexdigest()}"
        )


class HttpGroup(ToolGroup):
    """HTTP client tools: raw requests, readable page fetches, and downloads into the workspace."""

    name = "http"
    Settings = HttpSettings
    settings: HttpSettings

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt, self.settings)
