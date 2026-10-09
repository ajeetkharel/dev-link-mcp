// Activity: a virtual list over a capped window of event summaries, paged by id from the server.
import { getJSON, query } from "./api.js";
import { $, h } from "./dom.js";
import { fmtMs, fmtTime } from "./fmt.js";
import { debounce, elapsed, ensureTicker, pill, showError, throttle } from "./ui.js";
import { VirtualList } from "./vlist.js";

const PAGE = 200;
const CATCH_UP = 500;
// At most this many rows stay in memory; the far end is dropped and fetched again on scroll.
const CAP = 5000;
const ROW = 34;
// Load the next page this many rows before the end comes into view.
const AHEAD = 40;
const FILTERS = ["kind", "status", "name", "session", "client"];

const A = {
  rows: [], // newest first
  byId: new Map(),
  hasOlder: false,
  hasNewer: false,
  loading: false,
  seq: 0,
  filters: { kind: "", status: "", name: "", session: "", client: "", q: "" },
  paused: false,
  pending: [],
  overflow: false,
  newCount: 0,
  sel: null,
  incoming: [],
  flushQueued: false,
};
let list = null;
let feed = null;
let empty = null;
let openEvent = () => {};

function params() {
  const p = {};
  for (const k of FILTERS) p[k] = A.filters[k];
  p.q = A.filters.q;
  return p;
}

function matches(e) {
  for (const k of FILTERS) if (A.filters[k] && e[k] !== A.filters[k]) return false;
  return true;
}

/** Index of `id` in the newest-first rows, or the index where it would be inserted. */
function position(id) {
  let lo = 0, hi = A.rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (A.rows[mid].id > id) lo = mid + 1; else hi = mid;
  }
  return lo;
}

function renderRow(i) {
  const e = A.rows[i];
  const args = e.args_preview || "";
  return h("div", {
    class: "agrid arow " + (e.status || "") + (A.sel === e.id ? " sel" : ""),
    role: "row", "data-id": e.id,
  },
    h("span", { class: "t", title: new Date(e.ts * 1000).toString() }, fmtTime(e.ts)),
    h("span", { class: "kindc" }, h("span", { class: "kind" }, e.kind)),
    h("span", { class: "name", title: e.name }, e.name),
    h("span", { class: "client", title: e.client || "" }, e.client || "–"),
    h("span", null, pill(e.status)),
    h("span", { class: "dur" }, e.status === "running" ? elapsed(e.ts) : fmtMs(e.duration_ms)),
    h("span", { class: "args", title: args }, args === "{}" ? "" : args),
  );
}

function onPaint(first, last) {
  if (A.hasOlder && last >= A.rows.length - AHEAD) loadOlder();
  if (A.hasNewer && first <= AHEAD) loadNewer();
  if (feed.scrollTop < ROW && A.newCount && !A.hasNewer) { A.newCount = 0; renderPill(); }
  ensureTicker();
}

function renderFoot() {
  $("feed-count").textContent = A.rows.length
    ? A.rows.length.toLocaleString() + " events loaded" + (A.hasOlder ? "" : " · start of log")
    : "";
  const older = $("older");
  older.disabled = !A.hasOlder || A.loading;
  older.textContent = A.hasOlder ? "Load older" : "No older events";
  empty.hidden = A.rows.length > 0 || A.loading;
}

function renderPill() {
  const p = $("newpill");
  p.hidden = A.newCount <= 0;
  p.textContent = "↑ " + A.newCount.toLocaleString() + " new";
}

function renderPause() {
  const b = $("pause");
  b.classList.toggle("on", A.paused);
  b.textContent = !A.paused ? "Pause live" : A.pending.length || A.overflow
    ? "Resume (" + (A.overflow ? "many" : A.pending.length) + " new)" : "Resume live";
}

function index(rows) {
  A.byId.clear();
  for (const r of rows) A.byId.set(r.id, r);
}

function setRows(rows) {
  A.rows = rows;
  index(rows);
  list.reset();
  list.setCount(rows.length);
}

// Dropping rows above the viewport moves everything up, so scroll by the same amount.
function dropNewest(n) {
  for (const r of A.rows.splice(0, n)) A.byId.delete(r.id);
  feed.scrollTop = Math.max(0, feed.scrollTop - n * ROW);
  list.setCount(A.rows.length);
  A.hasNewer = true;
}

function dropOldest(n) {
  for (const r of A.rows.splice(A.rows.length - n, n)) A.byId.delete(r.id);
  list.setCount(A.rows.length);
  A.hasOlder = true;
}

/** Replace the window with the newest page matching the filters. */
export async function reload() {
  const seq = ++A.seq;
  A.loading = true;
  A.incoming = [];
  renderFoot();
  let rows;
  try {
    rows = await getJSON("events?" + query({ limit: PAGE, ...params() }));
  } catch (e) {
    if (seq === A.seq) { A.loading = false; renderFoot(); showError("Could not load events: " + e.message); }
    return;
  }
  if (seq !== A.seq) return;
  A.loading = false;
  A.hasOlder = rows.length === PAGE;
  A.hasNewer = false;
  A.newCount = 0;
  feed.scrollTop = 0;
  setRows(rows);
  renderFoot();
  renderPill();
}

async function loadOlder() {
  if (A.loading || !A.hasOlder || !A.rows.length) return;
  const seq = A.seq;
  A.loading = true;
  let rows;
  try {
    rows = await getJSON("events?" + query({ limit: PAGE, before: A.rows[A.rows.length - 1].id, ...params() }));
  } catch (e) {
    A.loading = false; renderFoot(); showError("Could not load older events: " + e.message);
    return;
  }
  A.loading = false;
  if (seq !== A.seq) return;
  for (const r of rows) if (!A.byId.has(r.id)) { A.rows.push(r); A.byId.set(r.id, r); }
  A.hasOlder = rows.length === PAGE;
  list.setCount(A.rows.length);
  if (A.rows.length > CAP) dropNewest(A.rows.length - CAP);
  renderFoot();
}

async function loadNewer() {
  if (A.loading || !A.hasNewer || !A.rows.length) return;
  const seq = A.seq;
  A.loading = true;
  let rows;
  try {
    rows = await getJSON("events?" + query({ limit: PAGE, after: A.rows[0].id, ...params() }));
  } catch (e) {
    A.loading = false; renderFoot(); showError("Could not load newer events: " + e.message);
    return;
  }
  A.loading = false;
  if (seq !== A.seq) return;
  const fresh = rows.filter((r) => !A.byId.has(r.id)).reverse();
  for (const r of fresh) A.byId.set(r.id, r);
  A.rows.unshift(...fresh);
  list.setCount(A.rows.length);
  feed.scrollTop += fresh.length * ROW;
  if (rows.length < PAGE) {
    A.hasNewer = false;
    A.newCount = 0;
    renderPill();
  }
  if (A.rows.length > CAP) dropOldest(A.rows.length - CAP);
  renderFoot();
}

function scheduleFlush() {
  if (A.flushQueued) return;
  A.flushQueued = true;
  requestAnimationFrame(flush);
}

// Applies queued live messages in one pass per frame. `trusted` items come from a server query
// with the current filters, so they are inserted without checking the filters again.
function flush() {
  A.flushQueued = false;
  const items = A.incoming;
  A.incoming = [];
  if (!items.length) return;
  const firstVisible = Math.floor(feed.scrollTop / ROW);
  const atTop = feed.scrollTop < ROW;
  let shift = 0;
  let catchUp = false;
  const changed = [];
  for (const { ev, trusted } of items) {
    const row = A.byId.get(ev.id);
    if (row) {
      Object.assign(row, ev);
      if (!trusted && !A.filters.q && !matches(row)) {
        const i = position(ev.id);
        A.rows.splice(i, 1);
        A.byId.delete(ev.id);
        if (!atTop && i < firstVisible) shift--;
      } else {
        changed.push(ev.id);
      }
      continue;
    }
    // The text filter also searches result bodies, which summaries do not carry: ask the server.
    if (!trusted && A.filters.q) { catchUp = true; continue; }
    if (!trusted && !matches(ev)) continue;
    const i = position(ev.id);
    if (i >= A.rows.length && A.hasOlder) continue; // older than the window; paging brings it
    if (A.hasNewer || (!atTop && A.rows.length >= CAP)) {
      if (i === 0) { A.hasNewer = true; A.newCount++; }
      continue;
    }
    A.rows.splice(i, 0, ev);
    A.byId.set(ev.id, ev);
    if (!atTop && i <= firstVisible) { shift++; A.newCount++; }
  }
  list.setCount(A.rows.length);
  if (shift) feed.scrollTop = Math.max(0, feed.scrollTop + shift * ROW);
  for (const id of changed) list.refresh(id);
  if (atTop && A.rows.length > CAP) dropOldest(A.rows.length - CAP);
  list.schedule();
  renderPill();
  renderFoot();
  if (catchUp) catchUpSoon();
}

/** Live audit message from the stream (an event started or finished). */
export function onAudit(ev) {
  if (A.paused) {
    if (A.pending.length < CAP) A.pending.push(ev); else A.overflow = true;
    renderPause();
    return;
  }
  A.incoming.push({ ev, trusted: false });
  scheduleFlush();
}

/**
 * Fetch what happened since the newest row, after a reconnect or while a text search is active.
 * Rows near the top that were still running are included, so their final status shows.
 */
export async function catchUp() {
  if (A.loading || A.hasNewer || A.paused) return;
  if (!A.rows.length || A.filters.status === "running") { await reload(); return; }
  let after = A.rows[0].id;
  for (let i = 0; i < Math.min(300, A.rows.length); i++) {
    if (A.rows[i].status === "running") after = Math.min(after, A.rows[i].id - 1);
  }
  const seq = A.seq;
  let rows;
  try {
    rows = await getJSON("events?" + query({ limit: CATCH_UP, after, ...params() }));
  } catch (_) { return; /* the stream reconnects and calls again */ }
  if (seq !== A.seq) return;
  if (rows.length === CATCH_UP) { await reload(); return; }
  for (const ev of rows) A.incoming.push({ ev, trusted: true });
  scheduleFlush();
}
const catchUpSoon = throttle(catchUp, 1000);

function renderTags() {
  const box = $("f-tags");
  box.replaceChildren();
  for (const key of ["session", "client"]) {
    const v = A.filters[key];
    if (!v) continue;
    box.append(h("span", { class: "filter-tag", title: v },
      h("span", null, key + " " + (key === "session" ? v.slice(0, 8) + "…" : v)),
      h("button", { type: "button", "aria-label": "Clear " + key + " filter", onclick: () => setFilter(key, "") }, "×")));
  }
}

/** Set one filter (kind, status, name, session, client or q) and reload from the server. */
export function setFilter(key, value) {
  A.filters[key] = value;
  if (key === "name") $("f-name").value = value;
  if (key === "kind") $("f-kind").value = value;
  if (key === "status") $("f-status").value = value;
  renderTags();
  reload();
}

/** Fill the tool filter with the registered tool names. */
export function setToolNames(names) {
  const sel = $("f-name");
  const cur = A.filters.name;
  sel.replaceChildren(h("option", { value: "" }, "All tools"), ...names.map((n) => h("option", { value: n }, n)));
  // A filter for a tool that is gone still applies; keep it selectable.
  if (cur && !names.includes(cur)) sel.append(h("option", { value: cur }, cur));
  sel.value = cur;
}

function select(id) {
  const prev = A.sel;
  A.sel = id;
  if (prev !== null) list.element(prev)?.classList.remove("sel");
  if (id !== null) list.element(id)?.classList.add("sel");
}

function onKey(k) {
  if (!A.rows.length) return;
  const cur = A.sel === null ? -1 : position(A.sel);
  const page = Math.max(1, Math.floor(feed.clientHeight / ROW) - 1);
  let i;
  if (k.key === "ArrowDown") i = Math.min(A.rows.length - 1, cur + 1);
  else if (k.key === "ArrowUp") i = Math.max(0, cur - 1);
  else if (k.key === "PageDown") i = Math.min(A.rows.length - 1, cur + page);
  else if (k.key === "PageUp") i = Math.max(0, cur - page);
  else if (k.key === "Home") i = 0;
  else if (k.key === "End") i = A.rows.length - 1;
  else if (k.key === "Enter" && A.sel !== null) { k.preventDefault(); openEvent(A.sel); return; }
  else return;
  k.preventDefault();
  select(A.rows[i].id);
  list.reveal(i);
}

/** Wire the Activity view. `open(id)` shows an event in the drawer. */
export function initActivity({ open }) {
  openEvent = open;
  feed = $("feed");
  empty = h("div", { class: "empty-state" }, "No events match these filters yet. Tool calls appear here as the agent makes them.");
  empty.hidden = true;
  feed.append(empty);
  list = new VirtualList(feed, { rowHeight: ROW, key: (i) => A.rows[i].id, render: renderRow, onPaint });
  feed.addEventListener("click", (ev) => {
    const row = ev.target.closest(".arow");
    if (!row) return;
    const id = Number(row.dataset.id);
    select(id);
    openEvent(id);
  });
  feed.addEventListener("keydown", onKey);
  $("f-kind").addEventListener("change", (e) => setFilter("kind", e.target.value));
  $("f-status").addEventListener("change", (e) => setFilter("status", e.target.value));
  $("f-name").addEventListener("change", (e) => setFilter("name", e.target.value));
  $("f-q").addEventListener("input", debounce((e) => setFilter("q", e.target.value.trim()), 300));
  $("older").addEventListener("click", loadOlder);
  $("newpill").addEventListener("click", () => {
    if (A.hasNewer) reload(); else { feed.scrollTop = 0; A.newCount = 0; renderPill(); }
  });
  $("pause").addEventListener("click", () => {
    A.paused = !A.paused;
    if (!A.paused) {
      const pending = A.pending, overflow = A.overflow;
      A.pending = [];
      A.overflow = false;
      if (overflow) reload();
      else { for (const ev of pending) A.incoming.push({ ev, trusted: false }); scheduleFlush(); }
    }
    renderPause();
    document.dispatchEvent(new CustomEvent("live-paused", { detail: A.paused }));
  });
  renderFoot();
}

export const isPaused = () => A.paused;
