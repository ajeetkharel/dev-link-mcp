"""Headless Chromium driven through Playwright for frontend work.

Each server has one browser, shared by all its pages and launched on first use. Each page
gets its own browser context so device emulation, viewport and the network guard apply per page.
Pages live until browser_close or server shutdown, so the agent can drive multi-step
flows against a dev server it started with start_process. After `idle_timeout_s` without any
browser call the whole browser is closed to free its memory and relaunches on the next call.

Chromium runs on the host, not inside bubblewrap, so it must never load file:// URLs:
a file page could navigate anywhere on disk. Agents serve local files over HTTP instead.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import functools
import ipaddress
import itertools
import json
import logging
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal, ParamSpec, TypeVar
from urllib.parse import urlsplit

import mcp.types as types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import NETWORK_OFF_REFUSED_HINT, OPEN_WORLD, READ_ONLY, WRITE, clip

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Request, Response, ViewportSize

log = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")

# `dev-link install-browser` runs Playwright from dev-link's own interpreter, however dev-link was installed.
INSTALL_HINT = "Install the browser with `dev-link install-browser` on the server host."
# Per-page event buffers; old entries fall off so a chatty page cannot grow memory forever.
MAX_EVENTS = 1000
SNAPSHOT_TEXT_CHARS = 30_000
SNAPSHOT_MAX_ITEMS = 150
# Console text kept per entry; a page logging megabyte strings must not flood memory or results.
MAX_EVENT_CHARS = 4000
# Console errors listed in an action result; browser_console has the full record.
MAX_SUMMARY_ERRORS = 20
# Bound on page.title(), which hangs while page scripts block the main thread.
TITLE_TIMEOUT_S = 5.0
# Inline screenshots larger than this are only saved to disk; huge base64 blobs flood the agent context.
MAX_INLINE_IMAGE_BYTES = 5_000_000
# Page ids remembered after an idle close; older ones get the generic "no open page" error.
MAX_IDLE_CLOSED_IDS = 100

LoadState = Literal["load", "domcontentloaded", "networkidle"]
WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]

# Same annotation as WRITE but the tool touches the network.
_OPEN_WORLD_WRITE = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

PageId = Annotated[str, Field(description="Page id returned by browser_open (e.g. 'p1').")]
Selector = Annotated[
    str,
    Field(
        description="Playwright selector: CSS ('#email', 'form button'), 'text=Sign in', or"
        " 'role=button[name=\"Save\"]'."
    ),
]
TimeoutS = Annotated[float, Field(gt=0, description="Seconds to wait before failing; capped at the server maximum.")]


@dataclass
class _PageState:
    """A tracked page and everything it has logged since it opened."""

    id: str
    page: Page
    context: BrowserContext
    seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    console: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    requests: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    # Keyed by the Request object itself; id() values can be reused once a request is collected.
    inflight: dict[Request, dict[str, Any]] = field(default_factory=dict)
    console_cleared: int = 0
    # Opened by the page's own script (target=_blank, window.open) rather than browser_open.
    popup: bool = False

    def next_seq(self) -> int:
        return next(self.seq)

    def last_seq(self) -> int:
        """Sequence number of the newest console entry (0 when none)."""
        return self.console[-1]["seq"] if self.console else 0


class _BrowserHost:
    """One server's Playwright driver, Chromium instance and page registry.

    With `idle_timeout_s` > 0 the driver and browser are closed once no browser tool call has run
    for that long, and start again on the next call.
    """

    def __init__(self, headless: bool, idle_timeout_s: float = 0) -> None:
        self.headless = headless
        self.idle_timeout_s = idle_timeout_s
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.pages: dict[str, _PageState] = {}
        self.counter = itertools.count(1)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock: asyncio.Lock | None = None
        self.sink: socket.socket | None = None
        self.active = 0
        self.idle_task: asyncio.Task[None] | None = None
        self.idle_closing = False
        # Ids of pages closed by the idle timeout, oldest first, so a late call gets a clear error.
        self.idle_closed: dict[str, None] = {}
        # browser_open calls past the page limit check that have not registered their page yet.
        self.opening = 0
        # Closes started from Playwright event handlers; kept here so they are not garbage collected.
        self.tasks: set[asyncio.Task[None]] = set()

    def tracked(self, fn: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
        """Wrap a browser tool so it counts as browser use for the idle timeout."""

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            lock = self._bind_loop()
            self.active += 1
            self._cancel_idle_timer()
            try:
                # An idle close in progress holds the lock; wait for it so this call relaunches.
                async with lock:
                    pass
                return await fn(*args, **kwargs)
            finally:
                self.active -= 1
                # A call cancelled while it waits for an idle close gets here during that close; the
                # close task must stay in idle_task so shutdown() can wait for it.
                running = self.playwright is not None and not self.idle_closing
                if self.active == 0 and running and self.idle_timeout_s > 0:
                    self._cancel_idle_timer()
                    self.idle_task = asyncio.create_task(self._close_when_idle())

        return wrapper

    def _cancel_idle_timer(self) -> None:
        # A close that has started is never cancelled: it would leave a half-closed browser.
        if self.idle_task is not None and not self.idle_closing:
            self.idle_task.cancel()
            self.idle_task = None

    async def _close_when_idle(self) -> None:
        await asyncio.sleep(self.idle_timeout_s)
        assert self.lock is not None
        async with self.lock:
            if self.active:
                return
            self.idle_closing = True
            try:
                for page_id in self.pages:
                    self.idle_closed[page_id] = None
                while len(self.idle_closed) > MAX_IDLE_CLOSED_IDS:
                    del self.idle_closed[next(iter(self.idle_closed))]
                await self._close_all()
                log.info("closed the browser after %gs without browser use", self.idle_timeout_s)
            finally:
                self.idle_closing = False
                self.idle_task = None

    def sink_proxy(self) -> str:
        """Proxy URL that refuses every connection, used to cut off non-loopback traffic.

        The port is bound but never listened on, so nothing else on the host can take it
        over and act as a real proxy.
        """
        if self.sink is None:
            self.sink = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sink.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{self.sink.getsockname()[1]}"

    def _bind_loop(self) -> asyncio.Lock:
        # Playwright objects belong to the loop that created them. A new loop (only seen in
        # tests) cannot reuse them, so start fresh instead of failing on every call.
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.playwright = None
            self.browser = None
            self.pages.clear()
            self.loop = loop
            self.lock = asyncio.Lock()
            self.active = 0
            self.idle_task = None
            self.idle_closing = False
            self.opening = 0
            self.tasks.clear()
        assert self.lock is not None
        return self.lock

    async def ensure_browser(self) -> Browser:
        """Return the running Chromium, starting Playwright and the browser on first use.

        Raises ToolError with install instructions when Playwright or Chromium is missing.
        """
        async with self._bind_loop():
            if self.browser is not None and self.browser.is_connected():
                return self.browser
            # A crashed browser takes its pages with it.
            self.pages.clear()
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise ToolError(
                    "Playwright is not installed. Install it with `pip install 'dev-link-mcp[browser]'`,"
                    f" then: {INSTALL_HINT}"
                ) from exc
            if self.playwright is None:
                try:
                    self.playwright = await async_playwright().start()
                except Exception as exc:
                    raise ToolError(f"Playwright could not start: {_first_line(exc)}. {INSTALL_HINT}") from exc
            try:
                self.browser = await self.playwright.chromium.launch(headless=self.headless)
            except Exception as exc:
                raise ToolError(f"Chromium could not launch: {_first_line(exc)}. {INSTALL_HINT}") from exc
            return self.browser

    def device(self, name: str) -> dict[str, Any]:
        """Playwright device descriptor by name, or ToolError with close matches."""
        assert self.playwright is not None
        devices = self.playwright.devices
        if name in devices:
            return dict(devices[name])
        close = difflib.get_close_matches(name, list(devices), n=5, cutoff=0.4)
        hint = f" Close matches: {', '.join(close)}." if close else ""
        raise ToolError(f"unknown device {name!r}.{hint} Names follow Playwright's list, e.g. 'iPhone 13', 'Pixel 7'.")

    def get(self, page_id: str) -> _PageState:
        self._bind_loop()
        page_id = page_id.strip()
        state = self.pages.get(page_id)
        if state is None or state.page.is_closed():
            if state is not None:
                self.forget(state)
            if page_id in self.idle_closed:
                raise ToolError(
                    f"page {page_id} was closed after {self.idle_timeout_s:g} s without browser use;"
                    " open it again with browser_open"
                )
            known = ", ".join(self.pages) or "none"
            raise ToolError(f"no open page with id {page_id!r} (open pages: {known}). Use browser_open first.")
        return state

    def live_pages(self) -> list[_PageState]:
        return [s for s in self.pages.values() if not s.page.is_closed()]

    def spawn(self, close: Awaitable[None]) -> None:
        """Run a close from a synchronous Playwright event handler, ignoring its errors."""

        async def run() -> None:
            # The page or context may already be gone with a crashed or closed browser.
            with contextlib.suppress(Exception):
                await close

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def forget(self, state: _PageState) -> None:
        """Stop tracking a page that is closed or closing. When it was the last tracked page of its
        context, close the context in the background so its cookies, storage and memory go too.
        Does nothing for a page that is no longer tracked."""
        if self.pages.get(state.id) is not state:
            return
        del self.pages[state.id]
        if not any(s.context is state.context for s in self.pages.values()):
            self.spawn(state.context.close())

    async def close_page(self, state: _PageState) -> None:
        """Close the page's whole context: the page and every popup sharing it stop being tracked."""
        for other in list(self.pages.values()):
            if other.context is state.context:
                del self.pages[other.id]
        # The context may already be gone with a crashed browser.
        with contextlib.suppress(Exception):
            await state.context.close()

    async def shutdown(self) -> None:
        """Stop the idle timer and close every page, the browser and the driver. Safe to call when
        nothing was started."""
        if self.loop is not None and self.loop is not asyncio.get_running_loop():
            # Objects from a dead loop cannot be awaited here; drop them.
            self.loop = None
            self.playwright = None
            self.browser = None
            self.pages.clear()
            self.idle_task = None
            return
        task = self.idle_task
        self._cancel_idle_timer()
        if task is not None:
            # Wait for a cancelled timer to finish, and let a running idle close complete rather
            # than close the same objects twice.
            await asyncio.wait({task})
        await self._close_all()

    async def _close_all(self) -> None:
        for state in list(self.pages.values()):
            await self.close_page(state)
        if self.tasks:
            await asyncio.wait(set(self.tasks))
        if self.browser is not None:
            with contextlib.suppress(Exception):
                await self.browser.close()
        if self.playwright is not None:
            with contextlib.suppress(Exception):
                await self.playwright.stop()
        self.browser = None
        self.playwright = None
        self.pages.clear()
        if self.sink is not None:
            self.sink.close()
            self.sink = None


def _first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


def _playwright_error(action: str, exc: Exception) -> ToolError:
    """ToolError for a failed Playwright call, keeping the useful part of its long call log."""
    message = str(exc).strip() or type(exc).__name__
    return ToolError(f"{action} failed: {clip(message, 2000)}")


def _is_loopback_host(host: str) -> bool:
    host = host.lower().rstrip(".").strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _network_allowed(rt: Runtime, url: str) -> bool:
    """Whether the browser may load `url` under the server's network setting."""
    if rt.config.sandbox.network:
        return True
    parts = urlsplit(url)
    if parts.scheme in ("data", "blob", "about"):
        return True
    return _is_loopback_host(parts.hostname or "")


def _check_url(rt: Runtime, url: str) -> str:
    """Validate an agent-supplied URL before navigation. Returns the cleaned URL."""
    url = url.strip()
    if url == "about:blank":
        return url
    parts = urlsplit(url)
    if parts.scheme == "file":
        raise ToolError(
            "file:// URLs are not allowed because the browser runs outside the sandbox. Serve the files over HTTP,"
            " e.g. start_process with `python3 -m http.server 8000`, then open http://127.0.0.1:8000/."
        )
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError(f"URL must be http(s)://host/... or about:blank, got {url!r}")
    if not _network_allowed(rt, url):
        raise ToolError(
            f"network access is disabled on this server; only loopback URLs are allowed, not {parts.hostname}"
        )
    return url


def _timeout_ms(rt: Runtime, timeout_s: float) -> float:
    return min(timeout_s, rt.config.limits.max_timeout_s) * 1000


def _track(host: _BrowserHost, state: _PageState) -> None:
    """Record console output, page errors and network activity from now on."""
    page = state.page

    def on_console(msg: Any) -> None:
        loc = msg.location or {}
        where = f"{loc.get('url', '')}:{loc.get('lineNumber', '')}" if loc.get("url") else ""
        state.console.append(
            {"seq": state.next_seq(), "type": msg.type, "text": clip(msg.text, MAX_EVENT_CHARS), "location": where}
        )

    def on_page_error(error: Any) -> None:
        text = getattr(error, "message", None) or str(error)
        state.console.append(
            {"seq": state.next_seq(), "type": "pageerror", "text": clip(text, MAX_EVENT_CHARS), "location": ""}
        )

    def on_request(request: Request) -> None:
        entry: dict[str, Any] = {
            "method": request.method,
            "url": request.url,
            "type": request.resource_type,
            "status": None,
            "failure": None,
            "duration_ms": None,
            "_start": time.monotonic(),
        }
        state.inflight[request] = entry
        state.requests.append(entry)

    def on_response(response: Response) -> None:
        entry = state.inflight.get(response.request)
        if entry is not None:
            entry["status"] = response.status

    def finish(request: Request, failure: str | None) -> None:
        entry = state.inflight.pop(request, None)
        if entry is not None:
            entry["duration_ms"] = round((time.monotonic() - entry["_start"]) * 1000)
            entry["failure"] = failure

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfinished", lambda request: finish(request, None))
    page.on("requestfailed", lambda request: finish(request, request.failure or "failed"))

    def on_close(_page: Any) -> None:
        # Covers pages that close themselves with window.close(), which would otherwise keep
        # their context open until browser_close or the idle timeout.
        host.forget(state)

    page.on("close", on_close)


def _register_page(host: _BrowserHost, page: Page, context: BrowserContext, *, popup: bool = False) -> _PageState:
    state = _PageState(id=f"p{next(host.counter)}", page=page, context=context, popup=popup)
    host.pages[state.id] = state
    _track(host, state)
    return state


async def _new_context(
    rt: Runtime, host: _BrowserHost, *, width: int, height: int, device: str | None, default_timeout_s: float
) -> BrowserContext:
    """A fresh context with the network guard installed. Popups opened from it are tracked too."""
    browser = await host.ensure_browser()
    options: dict[str, Any] = {"viewport": {"width": width, "height": height}}
    if device:
        options = host.device(device)
    # Downloads would land in a Playwright temp dir outside the workspace; the agent uses download_file.
    options["accept_downloads"] = False
    if not rt.config.sandbox.network:
        # Routes do not see redirect hops, service worker fetches or prefetches, so a proxy that
        # refuses connections backs them up for everything except loopback.
        options["proxy"] = {"server": host.sink_proxy(), "bypass": "localhost,*.localhost,127.0.0.0/8,[::1]"}
    context = await browser.new_context(**options)
    try:
        await _guard_context(rt, context, default_timeout_s)
    except BaseException:
        await asyncio.shield(_close_quietly(context))
        raise
    return context


async def _close_quietly(context: BrowserContext) -> None:
    with contextlib.suppress(Exception):
        await context.close()


async def _guard_context(rt: Runtime, context: BrowserContext, default_timeout_s: float) -> None:
    context.set_default_timeout(default_timeout_s * 1000)
    if not rt.config.sandbox.network:
        # Requests the route sees fail with a clear blockedbyclient error instead of a proxy error.

        async def block(route: Any) -> None:
            await route.abort("blockedbyclient")

        async def block_ws(ws: Any) -> None:
            await ws.close(code=1008, reason="network access is disabled on this server")

        def denied(url: str) -> bool:
            return not _network_allowed(rt, url)

        await context.route(denied, block)
        await context.route_web_socket(denied, block_ws)


async def _title(page: Page) -> str:
    try:
        return await asyncio.wait_for(page.title(), TITLE_TIMEOUT_S)
    except Exception:  # the page may be mid-navigation or busy running script
        return ""


async def _bounded(rt: Runtime, action: str, awaitable: Any, timeout_s: float) -> Any:
    """Await a Playwright call that has no timeout option of its own (evaluate, viewport).

    Raises ToolError on timeout or on any Playwright failure.
    """
    seconds = min(timeout_s, rt.config.limits.max_timeout_s)
    try:
        return await asyncio.wait_for(awaitable, seconds)
    except TimeoutError as exc:
        raise ToolError(f"{action} did not finish within {seconds:g}s") from exc
    except Exception as exc:
        raise _playwright_error(action, exc) from exc


def _errors_since(state: _PageState, mark: int) -> list[str]:
    return [
        f"[{e['type']}] {e['text']}" for e in state.console if e["seq"] > mark and e["type"] in ("error", "pageerror")
    ]


async def _page_summary(host: _BrowserHost, state: _PageState, mark: int, **extra: Any) -> dict[str, Any]:
    """Common result after an action: where the page is now and errors logged during the action."""
    result: dict[str, Any] = {"page_id": state.id, "url": state.page.url, "title": await _title(state.page)}
    result.update(extra)
    errors = _errors_since(state, mark)
    result["new_console_errors"] = errors[-MAX_SUMMARY_ERRORS:]
    if len(errors) > MAX_SUMMARY_ERRORS:
        result["new_console_errors_total"] = len(errors)
    popups = [s.id for s in host.pages.values() if s.context is state.context and s is not state]
    if popups:
        result["other_pages_in_context"] = popups
    return result


async def _goto(rt: Runtime, state: _PageState, url: str, wait_until: WaitUntil, timeout_s: float) -> int | None:
    try:
        response = await state.page.goto(url, wait_until=wait_until, timeout=_timeout_ms(rt, timeout_s))
    except Exception as exc:
        error = _playwright_error(f"navigating to {url}", exc)
        if not rt.config.sandbox.network and "ERR_CONNECTION_REFUSED" in str(exc):
            error = ToolError(f"{error}\n{NETWORK_OFF_REFUSED_HINT}")
        raise error from exc
    return response.status if response is not None else None


# Collects what an agent needs to act on a page: text, links, buttons and form fields,
# each with a selector that matches exactly one element where possible.
_SNAPSHOT_JS = """
(maxItems) => {
  const esc = (s) => CSS.escape(s);
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch { return false; } };
  const selectorFor = (el) => {
    const tag = el.tagName.toLowerCase();
    if (el.id && unique('#' + esc(el.id))) return '#' + esc(el.id);
    for (const attr of ['data-testid', 'data-test', 'name', 'aria-label']) {
      const v = el.getAttribute(attr);
      if (v) {
        const sel = `${tag}[${attr}="${v.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"')}"]`;
        if (unique(sel)) return sel;
      }
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node !== el && node.id && unique('#' + esc(node.id))) { parts.unshift('#' + esc(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const short = (s, n = 80) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, n);
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.labels && el.labels.length) return el.labels[0].innerText;
    return el.getAttribute('placeholder') || el.getAttribute('title') || '';
  };
  const collect = (sel, fn) => {
    const all = Array.from(document.querySelectorAll(sel)).filter(visible);
    return { total: all.length, items: all.slice(0, maxItems).map(fn) };
  };
  return {
    text: document.body ? document.body.innerText : '',
    links: collect('a[href]', (a) => ({ text: short(a.innerText || a.getAttribute('aria-label')),
                                        href: a.getAttribute('href'), selector: selectorFor(a) })),
    buttons: collect('button, input[type=button], input[type=submit], input[type=reset], [role=button]',
      (b) => ({ text: short(b.innerText || b.value || b.getAttribute('aria-label')), selector: selectorFor(b),
                disabled: !!b.disabled })),
    fields: collect('input:not([type=button]):not([type=submit]):not([type=reset]):not([type=hidden]), textarea,'
                    + ' select, [contenteditable=""], [contenteditable=true]',
      (f) => {
        const tag = f.tagName.toLowerCase();
        const item = { tag, type: f.type || '', name: f.name || '', label: short(labelFor(f)),
                       selector: selectorFor(f) };
        if (tag === 'select') {
          item.value = f.value;
          item.options = Array.from(f.options).slice(0, 30).map((o) => short(o.value, 40));
        } else if (f.type === 'checkbox' || f.type === 'radio') {
          item.checked = f.checked;
        } else if (f.type !== 'password') {
          item.value = short(f.value ?? f.innerText, 120);
        }
        if (f.required) item.required = true;
        if (f.disabled) item.disabled = true;
        return item;
      }),
  };
}
"""


def _format_snapshot(state: _PageState, title: str, data: dict[str, Any], limit: int) -> str:
    lines = [f"page {state.id}: {title or '(no title)'}", f"url: {state.page.url}", ""]

    def section(name: str, rows: list[str], total: int) -> None:
        more = f" (showing {len(rows)})" if total > len(rows) else ""
        lines.append(f"## {name}: {total}{more}")
        lines.extend(rows or ["(none)"])
        lines.append("")

    fields = []
    for f in data["fields"]["items"]:
        desc = f"{f['tag']}" + (f"[type={f['type']}]" if f["type"] and f["tag"] == "input" else "")
        attrs = [f"label={f['label']!r}"] if f["label"] else []
        if f["name"]:
            attrs.append(f"name={f['name']!r}")
        for key in ("value", "checked", "options", "required", "disabled"):
            if key in f:
                attrs.append(f"{key}={f[key]!r}")
        fields.append(f"- {desc} {' '.join(attrs)} -> {f['selector']}")
    section("form fields", fields, data["fields"]["total"])
    buttons = [
        f"- {b['text']!r}{' (disabled)' if b['disabled'] else ''} -> {b['selector']}" for b in data["buttons"]["items"]
    ]
    section("buttons", buttons, data["buttons"]["total"])
    links = [f"- {a['text']!r} href={a['href']} -> {a['selector']}" for a in data["links"]["items"]]
    section("links", links, data["links"]["total"])
    text = data["text"].strip()
    clipped = clip(text, SNAPSHOT_TEXT_CHARS)
    note = " (clipped; use browser_eval to read a specific element)" if clipped != text else ""
    lines.append(f"## visible text{note}")
    lines.append(clipped or "(empty)")
    return clip("\n".join(lines), limit)


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    headless: bool = Field(
        default=True, description="Run Chromium without a window. Set to false to watch the agent work on a desktop."
    )
    max_pages: int = Field(default=10, ge=1, description="Most pages, popups included, that may be open at once.")
    default_timeout_s: float = Field(
        default=15.0,
        gt=0,
        description="Seconds a browser action waits (for an element, a load, a script) when the agent does not "
        "set a timeout. `limits.max_timeout_s` still caps it.",
    )
    idle_timeout_s: float = Field(
        default=300.0,
        ge=0,
        description="Seconds without any browser tool call after which all pages, Chromium and the Playwright "
        "driver are closed to free their memory. The next browser call starts them again; pages open at the time "
        "must be opened again. 0 keeps them running until the server stops.",
    )


def _register(mcp: MCPServer, rt: Runtime, settings: BrowserSettings, host: _BrowserHost) -> None:
    max_pages = settings.max_pages
    default_timeout_s = settings.default_timeout_s
    limit = rt.config.limits.max_output_bytes

    def full() -> bool:
        return len(host.live_pages()) + host.opening >= max_pages

    async def open_page(url: str, width: int, height: int, device: str | None) -> _PageState:
        await host.ensure_browser()
        if full():
            live = ", ".join(s.id for s in host.live_pages())
            raise ToolError(f"{max_pages} pages are already open ({live}); close one first.")
        # Reserve the slot before the first await so concurrent opens cannot all pass the check.
        host.opening += 1
        try:
            context = await _new_context(
                rt, host, width=width, height=height, device=device, default_timeout_s=default_timeout_s
            )
            try:
                page = await context.new_page()
            except Exception as exc:
                await _close_quietly(context)
                raise _playwright_error("opening a page", exc) from exc
            except BaseException:
                await asyncio.shield(_close_quietly(context))
                raise
            state = _register_page(host, page, context)
        finally:
            host.opening -= 1

        # Popups (target=_blank, window.open) become addressable pages too, within the page limit.
        def on_popup(popup: Page) -> None:
            if not full():
                _register_page(host, popup, context, popup=True)
                return
            # A script calling window.open() in a loop would otherwise start renderers without bound.
            host.spawn(popup.close())
            message = f"a popup was closed because {max_pages} pages are already open; close one first"
            state.console.append({"seq": state.next_seq(), "type": "error", "text": message, "location": ""})

        context.on("page", on_popup)
        return state

    @mcp.tool(annotations=_OPEN_WORLD_WRITE)
    @host.tracked
    async def browser_open(
        url: Annotated[str, Field(description="http(s) URL to load, e.g. http://127.0.0.1:5173/, or about:blank.")],
        width: Annotated[int, Field(ge=200, le=7680, description="Viewport width in CSS pixels.")] = 1280,
        height: Annotated[int, Field(ge=200, le=4320, description="Viewport height in CSS pixels.")] = 800,
        device: Annotated[
            str | None,
            Field(description="Emulate a Playwright device such as 'iPhone 13' or 'Pixel 7' (overrides width/height)."),
        ] = None,
        wait_until: Annotated[WaitUntil, Field(description="Load event to wait for before returning.")] = "load",
        timeout_s: TimeoutS = default_timeout_s,
    ) -> dict[str, Any]:
        """Open a new page in the shared Chromium and load a URL. Returns the page id used by the other
        browser_* tools, plus title, HTTP status and console errors so far; console, page errors and
        network requests are recorded from the start. Pages stay open until browser_close, or until the
        browser closes after the server's idle timeout. browser_list_pages shows the page limit. Only
        http(s) URLs; serve local files over HTTP with start_process."""
        url = _check_url(rt, url)
        state = await open_page(url, width, height, device)
        try:
            status = await _goto(rt, state, url, wait_until, timeout_s) if url != "about:blank" else None
            return await _page_summary(host, state, 0, status=status)
        except BaseException:
            # Shielded so a cancelled call still frees the page and its slot under max_pages.
            await asyncio.shield(host.close_page(state))
            raise

    @mcp.tool(annotations=_OPEN_WORLD_WRITE)
    @host.tracked
    async def browser_navigate(
        page_id: PageId,
        url: Annotated[str, Field(description="http(s) URL to load in this page.")],
        wait_until: Annotated[WaitUntil, Field(description="Load event to wait for before returning.")] = "load",
        timeout_s: TimeoutS = default_timeout_s,
    ) -> dict[str, Any]:
        """Load a URL in an existing page, keeping its cookies and storage. Returns the new title,
        HTTP status and console errors logged during the navigation."""
        state = host.get(page_id)
        url = _check_url(rt, url)
        mark = state.last_seq()
        status = await _goto(rt, state, url, wait_until, timeout_s) if url != "about:blank" else None
        return await _page_summary(host, state, mark, status=status)

    @mcp.tool(annotations=OPEN_WORLD)
    @host.tracked
    async def browser_click(
        page_id: PageId, selector: Selector, timeout_s: TimeoutS = default_timeout_s
    ) -> dict[str, Any]:
        """Click the first element matching the selector, waiting for it to be visible and enabled.
        Returns the page url/title afterwards and console errors raised by the click. A click that starts
        a navigation does not wait for it to finish; follow up with browser_wait if needed."""
        state = host.get(page_id)
        mark = state.last_seq()
        try:
            await state.page.locator(selector).first.click(timeout=_timeout_ms(rt, timeout_s))
        except Exception as exc:
            raise _playwright_error(f"click on {selector!r}", exc) from exc
        return await _page_summary(host, state, mark)

    @mcp.tool(annotations=OPEN_WORLD)
    @host.tracked
    async def browser_type(
        page_id: PageId,
        selector: Selector,
        text: Annotated[str, Field(description="Text to put in the field. Replaces the current value.")],
        submit: Annotated[bool, Field(description="Press Enter afterwards (submits most forms).")] = False,
        timeout_s: TimeoutS = default_timeout_s,
    ) -> dict[str, Any]:
        """Fill an input, textarea or contenteditable element with text, replacing what was there, and
        fire input/change events. Set submit=True to press Enter afterwards. Use browser_press for
        individual keys or shortcuts."""
        state = host.get(page_id)
        mark = state.last_seq()
        locator = state.page.locator(selector).first
        try:
            await locator.fill(text, timeout=_timeout_ms(rt, timeout_s))
            if submit:
                await locator.press("Enter", timeout=_timeout_ms(rt, timeout_s))
        except Exception as exc:
            raise _playwright_error(f"typing into {selector!r}", exc) from exc
        return await _page_summary(host, state, mark)

    @mcp.tool(annotations=OPEN_WORLD)
    @host.tracked
    async def browser_press(
        page_id: PageId,
        key: Annotated[
            str,
            Field(description="Key or chord in Playwright syntax: 'Enter', 'Escape', 'Tab', 'ArrowDown', 'Control+a'."),
        ],
        selector: Annotated[
            str | None, Field(description="Focus this element first; default is the focused one.")
        ] = None,
        timeout_s: TimeoutS = default_timeout_s,
    ) -> dict[str, Any]:
        """Press a key or key chord on the page (or on a given element). Returns the page state and
        console errors raised by the key press."""
        state = host.get(page_id)
        mark = state.last_seq()
        try:
            if selector:
                await state.page.locator(selector).first.press(key, timeout=_timeout_ms(rt, timeout_s))
            else:
                await state.page.keyboard.press(key)
        except Exception as exc:
            raise _playwright_error(f"pressing {key!r}", exc) from exc
        return await _page_summary(host, state, mark)

    @mcp.tool(annotations=OPEN_WORLD)
    @host.tracked
    async def browser_select(
        page_id: PageId,
        selector: Selector,
        value: Annotated[str, Field(description="Option value or visible label to select.")],
        timeout_s: TimeoutS = default_timeout_s,
    ) -> dict[str, Any]:
        """Choose an option in a <select> element by its value or label. Returns the selected values."""
        state = host.get(page_id)
        mark = state.last_seq()
        try:
            selected = await state.page.locator(selector).first.select_option(value, timeout=_timeout_ms(rt, timeout_s))
        except Exception as exc:
            raise _playwright_error(f"selecting {value!r} in {selector!r}", exc) from exc
        return await _page_summary(host, state, mark, selected=selected)

    @mcp.tool(annotations=READ_ONLY)
    @host.tracked
    async def browser_wait(
        page_id: PageId,
        selector: Annotated[str | None, Field(description="Wait until an element matching this is visible.")] = None,
        text: Annotated[str | None, Field(description="Wait until this text is visible on the page.")] = None,
        url_pattern: Annotated[
            str | None, Field(description="Wait until the page URL matches this glob, e.g. '**/dashboard'.")
        ] = None,
        load_state: Annotated[LoadState | None, Field(description="Wait for this load state.")] = None,
        timeout_s: TimeoutS = default_timeout_s,
    ) -> dict[str, Any]:
        """Wait for exactly one condition: a visible selector, visible text, a URL glob, or a load state
        ('networkidle' waits for 500ms without requests). Fails on timeout."""
        state = host.get(page_id)
        given = {
            k: v
            for k, v in {
                "selector": selector,
                "text": text,
                "url_pattern": url_pattern,
                "load_state": load_state,
            }.items()
            if v
        }
        if len(given) != 1:
            raise ToolError("give exactly one of selector, text, url_pattern or load_state")
        ((kind, target),) = given.items()
        mark = state.last_seq()
        timeout = _timeout_ms(rt, timeout_s)
        started = time.monotonic()
        try:
            if selector:
                await state.page.locator(selector).first.wait_for(state="visible", timeout=timeout)
            elif text:
                await state.page.get_by_text(text).first.wait_for(state="visible", timeout=timeout)
            elif url_pattern:
                await state.page.wait_for_url(url_pattern, timeout=timeout)
            else:
                await state.page.wait_for_load_state(load_state, timeout=timeout)
        except Exception as exc:
            raise _playwright_error(f"waiting for {kind} {target!r}", exc) from exc
        return await _page_summary(host, state, mark, waited_s=round(time.monotonic() - started, 2))

    @mcp.tool(annotations=_OPEN_WORLD_WRITE)
    @host.tracked
    async def browser_screenshot(
        page_id: Annotated[str | None, Field(description="Page to capture. Give this or url, not both.")] = None,
        url: Annotated[
            str | None, Field(description="Open this URL in a temporary page, capture it, and close it.")
        ] = None,
        selector: Annotated[str | None, Field(description="Capture only the first element matching this.")] = None,
        full_page: Annotated[
            bool, Field(description="Capture the whole scrollable page, not just the viewport.")
        ] = False,
        name: Annotated[str, Field(description="File name stem for the saved PNG.")] = "screenshot",
        width: Annotated[int, Field(ge=200, le=7680, description="Viewport width when url is used.")] = 1280,
        height: Annotated[int, Field(ge=200, le=4320, description="Viewport height when url is used.")] = 800,
        timeout_s: TimeoutS = default_timeout_s,
    ) -> list[types.ImageContent | types.TextContent]:
        """Take a PNG screenshot of an open page or of a URL. The image is returned inline and saved
        under .dev-link/artifacts (git-ignored); the text part gives the saved workspace-relative path."""
        if (page_id is None) == (url is None):
            raise ToolError("give exactly one of page_id or url")
        if full_page and selector:
            raise ToolError("full_page and selector cannot be combined")
        timeout = _timeout_ms(rt, timeout_s)
        temp: _PageState | None = None
        if url is not None:
            url = _check_url(rt, url)
            temp = await open_page(url, width, height, None)
            state = temp
        else:
            state = host.get(page_id or "")
        try:
            if temp is not None and url != "about:blank":
                await _goto(rt, temp, url or "", "load", timeout_s)
            try:
                if selector:
                    png = await state.page.locator(selector).first.screenshot(timeout=timeout)
                else:
                    png = await state.page.screenshot(full_page=full_page, timeout=timeout)
            except Exception as exc:
                raise _playwright_error("screenshot", exc) from exc
            page_url = state.page.url
        finally:
            if temp is not None:
                await asyncio.shield(host.close_page(temp))
        path = rt.workspace.artifact_path(name, ".png")
        await asyncio.to_thread(rt.workspace.write_file, path, png)
        where = f"page {state.id}" if temp is None else page_url
        line = f"screenshot of {where} saved to {rt.workspace.rel(path)} ({len(png)} bytes)"
        if len(png) > MAX_INLINE_IMAGE_BYTES:
            return [types.TextContent(type="text", text=f"{line}; too large to return inline, open the file instead")]
        image = types.ImageContent(type="image", data=base64.b64encode(png).decode(), mime_type="image/png")
        return [image, types.TextContent(type="text", text=line)]

    @mcp.tool(annotations=READ_ONLY)
    @host.tracked
    async def browser_snapshot(page_id: PageId) -> str:
        """Readable outline of the page for deciding what to do next: title, URL, visible form fields,
        buttons and links each with a selector usable in browser_click/browser_type, then the visible
        text (clipped). Password values are never included."""
        state = host.get(page_id)
        data = await _bounded(rt, "snapshot", state.page.evaluate(_SNAPSHOT_JS, SNAPSHOT_MAX_ITEMS), default_timeout_s)
        return _format_snapshot(state, await _title(state.page), data, limit)

    @mcp.tool(annotations=READ_ONLY)
    @host.tracked
    async def browser_console(
        page_id: PageId,
        clear: Annotated[
            bool, Field(description="Forget the returned entries so the next call shows only new ones.")
        ] = False,
    ) -> str:
        """Console messages (log, warning, error, ...) and uncaught page errors recorded since the page
        opened or the last clear, oldest first, each with its source location."""
        state = host.get(page_id)
        entries = list(state.console)
        lines = [f"[{e['type']}] {e['text']}" + (f"  ({e['location']})" if e["location"] else "") for e in entries]
        dropped = entries[0]["seq"] - state.console_cleared - 1 if entries else 0
        header = f"{len(entries)} console entries for page {state.id}"
        if dropped > 0:
            header += f" ({dropped} older entries dropped; the buffer keeps the last {MAX_EVENTS})"
        if clear:
            state.console_cleared = state.last_seq()
            state.console.clear()
        return clip("\n".join([header, *lines]), limit)

    @mcp.tool(annotations=READ_ONLY)
    @host.tracked
    async def browser_network(
        page_id: PageId,
        failed_only: Annotated[
            bool, Field(description="Only requests that failed to complete or got an HTTP status >= 400.")
        ] = True,
        max_entries: Annotated[int, Field(ge=1, le=MAX_EVENTS, description="Newest entries to return.")] = 200,
    ) -> dict[str, Any]:
        """Network requests made by the page since it opened: method, URL, resource type, HTTP status,
        failure reason and duration. By default only failures and >= 400 responses, newest last."""
        state = host.get(page_id)
        rows = [
            {k: v for k, v in entry.items() if not k.startswith("_")}
            for entry in state.requests
            if not failed_only or entry["failure"] or (entry["status"] or 0) >= 400
        ]
        shown = rows[-max_entries:]
        for row in shown:
            row["url"] = clip(row["url"], 500)
        return {"page_id": state.id, "total": len(rows), "returned": len(shown), "requests": shown}

    @mcp.tool(annotations=OPEN_WORLD)
    @host.tracked
    async def browser_eval(
        page_id: PageId,
        script: Annotated[
            str,
            Field(
                description="JavaScript expression or function, e.g. 'document.title' or "
                "'() => [...document.querySelectorAll(\"li\")].map(e => e.textContent)'. "
                "Promises are awaited."
            ),
        ],
        timeout_s: TimeoutS = default_timeout_s,
    ) -> str:
        """Run JavaScript in the page and return the result as JSON. It runs with the page's privileges,
        including its cookies, and can change page state. DOM nodes and other non-serializable values
        come back as null or {}."""
        state = host.get(page_id)
        mark = state.last_seq()
        value = await _bounded(rt, "script", state.page.evaluate(script), timeout_s)
        out = json.dumps(value, indent=2, default=str, ensure_ascii=False)
        errors = _errors_since(state, mark)
        if errors:
            out += "\n\nconsole errors during evaluation:\n" + "\n".join(errors)
        return clip(out, limit)

    @mcp.tool(annotations=WRITE)
    @host.tracked
    async def browser_set_viewport(
        page_id: PageId,
        width: Annotated[int, Field(ge=200, le=7680, description="Viewport width in CSS pixels.")],
        height: Annotated[int, Field(ge=200, le=4320, description="Viewport height in CSS pixels.")],
    ) -> dict[str, Any]:
        """Resize the page viewport, e.g. to check responsive breakpoints. Media queries re-evaluate
        immediately; the page is not reloaded."""
        state = host.get(page_id)
        mark = state.last_seq()
        size: ViewportSize = {"width": width, "height": height}
        await _bounded(rt, "resizing the viewport", state.page.set_viewport_size(size), default_timeout_s)
        return await _page_summary(host, state, mark, viewport={"width": width, "height": height})

    @mcp.tool(annotations=READ_ONLY)
    @host.tracked
    async def browser_list_pages() -> dict[str, Any]:
        """Open browser pages with their id, URL, title, viewport and number of console errors."""
        host._bind_loop()
        pages = []
        for state in list(host.pages.values()):
            if state.page.is_closed():
                host.forget(state)
                continue
            pages.append(
                {
                    "page_id": state.id,
                    "url": state.page.url,
                    "title": await _title(state.page),
                    "viewport": state.page.viewport_size,
                    "console_errors": len(_errors_since(state, 0)),
                }
            )
        return {"pages": pages, "max_pages": max_pages}

    @mcp.tool(annotations=WRITE)
    @host.tracked
    async def browser_close(page_id: PageId) -> str:
        """Close a page and discard its recorded logs. Closing a page from browser_open also discards its
        cookies and storage and closes the popups it opened; closing a popup closes only that popup."""
        state = host.get(page_id)
        if state.popup:
            host.forget(state)
            with contextlib.suppress(Exception):
                await state.page.close()
            return f"closed {state.id}"
        siblings = [s for s in list(host.pages.values()) if s.context is state.context]
        await host.close_page(state)
        return f"closed {', '.join(s.id for s in siblings)}"


class BrowserGroup(ToolGroup):
    """Headless Chromium driven through Playwright for frontend work."""

    name = "browser"
    Settings = BrowserSettings
    settings: BrowserSettings

    def __init__(self, rt: Runtime, settings: BaseModel) -> None:
        super().__init__(rt, settings)
        self.host = _BrowserHost(headless=self.settings.headless, idle_timeout_s=self.settings.idle_timeout_s)

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt, self.settings, self.host)

    async def shutdown(self) -> None:
        """Stop the idle timer and close every page, Chromium and the Playwright driver. Safe when the
        browser never started."""
        await self.host.shutdown()
