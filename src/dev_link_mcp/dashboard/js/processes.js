// Background processes and a log viewer that tails by byte offset into a ring buffer of lines.
import { getJSON, postJSON, query } from "./api.js";
import { $, h } from "./dom.js";
import { fmtMs } from "./fmt.js";
import { elapsed, ensureTicker, pill, showError, throttle } from "./ui.js";
import { VirtualList } from "./vlist.js";

const LINE = 18;
const MAX_LINES = 50000;
// Longer lines are cut for display; the full text stays in the log file.
const MAX_LINE_CHARS = 4000;
const START_BYTES = 256 * 1024;
const READ_MAX = 1000000;
// A gap larger than this is skipped instead of read, so a fast writer cannot bury the viewer.
const SKIP_OVER = 1000000;
// CSI sequences (colors, cursor moves), OSC titles and other two-byte escapes.
const ANSI = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;

/** Fixed-capacity buffer of lines; `first` is the absolute number of the oldest kept line. */
class Ring {
  constructor(cap) { this.cap = cap; this.buf = new Array(cap); this.start = 0; this.size = 0; this.first = 0; }
  push(line) {
    if (this.size < this.cap) { this.buf[(this.start + this.size) % this.cap] = line; this.size++; return 0; }
    this.buf[this.start] = line;
    this.start = (this.start + 1) % this.cap;
    this.first++;
    return 1;
  }
  get(i) { return this.buf[(this.start + i) % this.cap]; }
  clear() { this.buf = new Array(this.cap); this.start = 0; this.size = 0; this.first = 0; }
}

const P = {
  procs: [],
  visible: false,
  sel: null, // { id, offset, want, busy, partial, seq }
  seq: 0,
};
const ring = new Ring(MAX_LINES);
let list = null;
let hint = null;
let follow = true;

function renderLine(i) {
  const text = ring.get(i);
  if (text && text.skip) return h("div", { class: "skip" }, text.skip);
  return h("div", null, text);
}

function clean(line) {
  const t = line.endsWith("\r") ? line.slice(0, -1) : line;
  const plain = t.includes("\x1b") ? t.replace(ANSI, "") : t;
  return plain.length > MAX_LINE_CHARS ? plain.slice(0, MAX_LINE_CHARS) + " …" : plain;
}

function appendText(text, sel) {
  const parts = (sel.partial + text).split("\n");
  sel.partial = parts.pop();
  let dropped = 0;
  for (const p of parts) dropped += ring.push(clean(p));
  return dropped;
}

function appendMarker(label) {
  return ring.push({ skip: label });
}

function afterAppend(dropped) {
  hint.hidden = ring.size > 0;
  // Dropping the oldest lines shifts every index; keep the view still unless following.
  if (dropped && !follow) list.viewport.scrollTop = Math.max(0, list.viewport.scrollTop - dropped * LINE);
  list.setCount(ring.size);
  if (dropped) list.reset();
  if (follow) list.viewport.scrollTop = ring.size * LINE;
  list.schedule();
}

async function pump() {
  const sel = P.sel;
  if (!sel || sel.busy || !P.visible) return;
  sel.busy = true;
  try {
    while (P.sel === sel && P.visible && sel.offset < sel.want) {
      const gap = sel.want - sel.offset;
      let from = sel.offset;
      let dropped = 0;
      if (gap > SKIP_OVER) {
        from = sel.want - START_BYTES;
        if (sel.partial) dropped += appendText("\n", sel);
        dropped += appendMarker("… skipped " + Math.round((from - sel.offset) / 1024).toLocaleString() + " KB of output …");
        sel.skipLine = true;
      }
      const r = await getJSON("processes/" + encodeURIComponent(sel.id) + "/log?" + query({ offset: from, limit: Math.min(READ_MAX, sel.want - from) }));
      if (P.sel !== sel) return;
      let text = r.text || "";
      // After a skip the read starts mid-line; drop that partial line.
      if (sel.skipLine) { text = text.slice(text.indexOf("\n") + 1); sel.skipLine = false; }
      if (r.offset <= from) { afterAppend(dropped); break; } // nothing new
      sel.offset = r.offset;
      afterAppend(dropped + appendText(text, sel));
    }
  } catch (e) {
    if (P.sel === sel) hint.textContent = "Could not read log: " + e.message;
  } finally {
    sel.busy = false;
  }
}

async function selectProc(p) {
  const mine = ++P.seq;
  ring.clear();
  list.setCount(0);
  list.reset();
  hint.textContent = "Loading…";
  hint.hidden = false;
  follow = true;
  $("log-follow").checked = true;
  $("log-title").textContent = "Log · " + p.id + " · " + p.name;
  const start = Math.max(0, (p.log_bytes || 0) - START_BYTES);
  P.sel = { id: p.id, offset: start, want: p.log_bytes || 0, busy: false, partial: "", skipLine: start > 0, seq: mine };
  renderProcs();
  if (!p.log_bytes) { hint.textContent = "(no output yet)"; }
  // Read at least once even when the size is unknown or stale.
  P.sel.want = Math.max(P.sel.want, start + 1);
  await pump();
  if (P.sel && P.sel.seq === mine && !ring.size) hint.textContent = "(no output yet)";
}

function renderProcs() {
  const running = P.procs.filter((p) => p.status === "running").length;
  const c = $("proc-count");
  c.textContent = running;
  c.classList.toggle("hot", running > 0);
  $("procs-empty").hidden = P.procs.length > 0;
  $("procs").replaceChildren(...P.procs.slice().reverse().map((p) => {
    const isRun = p.status === "running";
    const stop = isRun ? h("button", { type: "button", class: "btn small danger", "data-stop": p.id }, "Stop") : null;
    return h("tr", { class: "click" + (P.sel && P.sel.id === p.id ? " sel" : ""), tabindex: "0", role: "row", "data-id": p.id },
      h("td", { class: "mono" }, p.id),
      h("td", null, p.name),
      h("td", null, isRun ? pill("running") : h("span", { class: "pill " + (/\(0\)$/.test(p.status) ? "ok" : "other") }, p.status)),
      h("td", { class: "num" }, isRun ? elapsed(p.started) : fmtMs((p.uptime_s || 0) * 1000)),
      h("td", { class: "cmd", title: p.command + "\n" + p.cwd }, p.command),
      h("td", null, stop));
  }));
  ensureTicker();
}

export async function load() {
  try {
    P.procs = await getJSON("processes");
  } catch (e) { showError("Could not load processes: " + e.message); return; }
  renderProcs();
  const sel = P.sel && P.procs.find((p) => p.id === P.sel.id);
  if (sel) { P.sel.want = Math.max(P.sel.want, sel.log_bytes || 0); pump(); }
}
const loadSoon = throttle(load, 300);

async function stopProc(id) {
  const p = P.procs.find((x) => x.id === id);
  if (!p || !window.confirm("Stop process " + p.id + " (" + p.name + ")?\n\n" + p.command)) return;
  try {
    await postJSON("processes/" + encodeURIComponent(p.id) + "/stop");
  } catch (e) { showError("Could not stop " + p.id + ": " + e.message); }
  load();
}

/** Live "process" message: a process started, exited or was stopped. */
export function onProcess() { loadSoon(); }

/** Live "log" message: process `id` wrote output up to byte `offset`. */
export function onLog({ id, offset }) {
  if (P.sel && P.sel.id === id && offset > P.sel.want) {
    P.sel.want = offset;
    pump();
  }
}

/** The tab became visible or hidden. While hidden the log is not read. */
export function setVisible(v) {
  P.visible = v;
  if (v) load();
}

export function initProcesses() {
  const log = $("log");
  hint = h("div", { class: "hint" }, "Select a process to tail its log.");
  log.append(hint);
  list = new VirtualList(log, { rowHeight: LINE, key: (i) => ring.first + i, render: renderLine, overscan: 10 });
  log.addEventListener("scroll", () => {
    // Scrolling up pauses follow; reaching the bottom again resumes it.
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - LINE;
    if (follow !== atBottom) { follow = atBottom; $("log-follow").checked = atBottom; }
  }, { passive: true });
  $("log-follow").addEventListener("change", (e) => {
    follow = e.target.checked;
    if (follow) log.scrollTop = log.scrollHeight;
  });
  const body = $("procs");
  body.addEventListener("click", (ev) => {
    const stop = ev.target.closest("[data-stop]");
    if (stop) { ev.stopPropagation(); stopProc(stop.dataset.stop); return; }
    const tr = ev.target.closest("tr[data-id]");
    const p = tr && P.procs.find((x) => x.id === tr.dataset.id);
    if (p) selectProc(p);
  });
  body.addEventListener("keydown", (k) => {
    if (k.key !== "Enter") return;
    const tr = k.target.closest("tr[data-id]");
    const p = tr && P.procs.find((x) => x.id === tr.dataset.id);
    if (p) selectProc(p);
  });
  $("proc-refresh").addEventListener("click", load);
}
