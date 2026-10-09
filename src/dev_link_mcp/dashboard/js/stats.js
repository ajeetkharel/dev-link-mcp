// Summary tiles, the calls-over-time chart and the per-tool table for the selected range.
import { getJSON } from "./api.js";
import { $, h, s } from "./dom.js";
import { fmtInt, fmtMs, pad2 } from "./fmt.js";
import { debounce, showError, throttle } from "./ui.js";

const RANGE_LABEL = { 3600: "Last hour", 21600: "Last 6 hours", 86400: "Last 24 hours", 604800: "Last 7 days" };
const S = { range: 86400, stats: null, sort: { key: "calls", dir: -1 }, seq: 0 };
const listeners = [];
let onFilter = () => {};

export const range = () => S.range;
export const current = () => S.stats;

/** Call `fn(stats)` after every refresh. */
export function onStats(fn) { listeners.push(fn); }

export async function load() {
  const mine = ++S.seq;
  let st;
  try {
    st = await getJSON("stats?since=" + S.range);
  } catch (e) { showError("Could not load stats: " + e.message); return; }
  if (mine !== S.seq) return;
  S.stats = st;
  renderTiles();
  renderChart();
  renderPerTool();
  for (const fn of listeners) fn(st);
}

// Stats are aggregates over the whole range, so a burst of calls refreshes them once.
const refreshSoon = throttle(load, 3000);

/** Live audit message: refresh once a call finishes. */
export function onAudit(ev) {
  if (ev.kind === "tool" || ev.kind === "session") refreshSoon();
}

function renderTiles() {
  const st = S.stats;
  const tools = st.per_tool || [];
  $("t-calls").textContent = fmtInt(st.calls);
  $("t-calls-s").textContent = tools.length + " distinct tools";
  $("t-err").textContent = fmtInt(st.errors);
  $("t-err-s").textContent = st.calls ? ((st.errors / st.calls) * 100).toFixed(1) + "% error rate" : "no calls";
  $("tile-err").classList.toggle("has-err", st.errors > 0);
  $("t-avg").textContent = fmtMs(st.avg_ms);
  const slowest = tools.reduce((m, t) => (t.max_ms > (m ? m.max_ms : -1) ? t : m), null);
  $("t-avg-s").textContent = slowest ? "slowest " + slowest.name + " " + fmtMs(slowest.max_ms) : "";
  $("t-run").textContent = fmtInt(st.running);
  $("t-run-s").textContent = st.running ? "calls in flight" : "idle";
  $("tile-run").classList.toggle("has-run", st.running > 0);
  $("t-cli").textContent = fmtInt(st.clients);
  $("t-cli-s").textContent = "by session, else client name";
}

function niceMax(v) {
  if (v <= 4) return 4;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function renderChart() {
  const box = $("chart"), tip = $("tip");
  for (const n of box.querySelectorAll("svg")) n.remove();
  const st = S.stats;
  if (!st) return;
  const W = Math.max(280, box.clientWidth - 20), H = 200, ml = 34, mr = 6, mt = 8, mb = 22;
  // The server buckets by 5 minutes; merge into wider buckets so long ranges stay readable.
  const size = Math.max(300, Math.ceil(S.range / 96 / 300) * 300);
  const now = Date.now() / 1000;
  const start = Math.floor((now - S.range) / size) * size;
  const buckets = [];
  for (let t = start; t <= now; t += size) buckets.push({ t, calls: 0, errors: 0 });
  for (const b of st.timeline || []) {
    const i = Math.floor((b.bucket - start) / size);
    if (i >= 0 && i < buckets.length) { buckets[i].calls += b.calls || 0; buckets[i].errors += b.errors || 0; }
  }
  let top = 0;
  for (const b of buckets) if (b.calls > top) top = b.calls;
  const max = niceMax(top);
  const iw = W - ml - mr, ih = H - mt - mb;
  const bw = iw / buckets.length;
  const gap = bw > 6 ? 2 : (bw > 3 ? 1 : 0);
  const y = (v) => mt + ih - (v / max) * ih;
  const svg = s("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Tool calls over time, failed calls stacked on top" });
  const grid = s("g", { class: "grid" }), axis = s("g", { class: "axis" });
  for (let k = 0; k <= 4; k++) {
    const v = (max / 4) * k, yy = Math.round(y(v)) + 0.5;
    grid.append(s("line", { x1: ml, x2: W - mr, y1: yy, y2: yy }));
    axis.append(s("text", { x: ml - 6, y: yy + 3.5, "text-anchor": "end" }, Number.isInteger(v) ? v : v.toFixed(1)));
  }
  const every = Math.max(1, Math.ceil(buckets.length / 6));
  const longRange = S.range > 86400;
  const dayLabel = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  buckets.forEach((b, i) => {
    if (i % every !== 0) return;
    const d = new Date(b.t * 1000);
    const label = longRange ? dayLabel(d) : pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    axis.append(s("text", { x: ml + i * bw + bw / 2, y: H - 6, "text-anchor": "middle" }, label));
  });
  svg.append(grid, axis);
  const marks = s("g"), hits = s("g");
  const fmtRange = (t) => {
    const a = new Date(t * 1000), z = new Date((t + size) * 1000);
    const f = (d) => pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    return (longRange ? dayLabel(a) + " " : "") + f(a) + "–" + f(z);
  };
  buckets.forEach((b, i) => {
    const x = ml + i * bw + gap / 2, w = Math.max(1, bw - gap);
    const okCount = b.calls - b.errors;
    const r = Math.min(3, w / 2);
    if (okCount > 0) {
      const t = y(okCount), hgt = mt + ih - t;
      // Round only the data end when no error segment sits on top.
      marks.append(s("rect", { class: "m-ok", x, y: t, width: w, height: hgt, rx: b.errors ? 0 : r }));
      if (!b.errors && hgt > r) marks.append(s("rect", { class: "m-ok", x, y: t + hgt - r, width: w, height: r }));
    }
    if (b.errors > 0) {
      const t = y(b.calls), base = y(okCount) - (okCount > 0 ? 2 : 0);
      const hgt = Math.max(1, base - t);
      marks.append(s("rect", { class: "m-err", x, y: t, width: w, height: hgt, rx: r }));
      if (hgt > r) marks.append(s("rect", { class: "m-err", x, y: t + hgt - r, width: w, height: r }));
    }
    const hit = s("rect", { class: "hit", x: ml + i * bw, y: mt, width: bw, height: ih });
    hit.addEventListener("mouseenter", () => {
      tip.replaceChildren(h("b", null, fmtRange(b.t)), h("br"),
        b.calls + " call" + (b.calls === 1 ? "" : "s") + (b.errors ? " · " + b.errors + " failed" : ""));
      const scale = svg.getBoundingClientRect().width / W || 1;
      tip.style.left = (10 + (ml + i * bw + bw / 2) * scale) + "px";
      tip.style.top = (8 + y(b.calls) * scale - 6) + "px";
      tip.hidden = false;
    });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; });
    hits.append(hit);
  });
  svg.append(marks, hits);
  if (!st.calls) svg.append(s("text", { class: "empty", x: ml + iw / 2, y: mt + ih / 2, "text-anchor": "middle" }, "No tool calls in this range"));
  box.prepend(svg);
}

function renderPerTool() {
  const rows = [...(S.stats.per_tool || [])];
  const { key, dir } = S.sort;
  rows.sort((a, b) => {
    const av = a[key] ?? -1, bv = b[key] ?? -1;
    if (typeof av === "string") return dir * av.localeCompare(bv);
    return dir * (av - bv);
  });
  let top = 1;
  for (const r of rows) if (r.calls > top) top = r.calls;
  for (const th of document.querySelectorAll("#pertool th")) {
    th.setAttribute("aria-sort", th.dataset.k === key ? (dir > 0 ? "ascending" : "descending") : "none");
  }
  const body = document.querySelector("#pertool tbody");
  if (!rows.length) {
    body.replaceChildren(h("tr", null, h("td", { colspan: "5", class: "muted" }, "No tool calls in this range.")));
    return;
  }
  body.replaceChildren(...rows.map((r) => {
    const share = h("span", { class: "share" });
    share.style.width = Math.round((r.calls / top) * 40) + "px";
    return h("tr", {
      class: "click", tabindex: "0", title: "Show " + r.name + " calls in the activity", "data-name": r.name,
    },
      h("td", { class: "mono" }, r.name),
      h("td", { class: "num" }, share, fmtInt(r.calls)),
      h("td", { class: "num" + (r.errors ? " err-text" : "") }, fmtInt(r.errors)),
      h("td", { class: "num" }, fmtMs(r.avg_ms)),
      h("td", { class: "num" }, fmtMs(r.max_ms)),
    );
  }));
}

/** Wire the overview. `filter(name)` shows one tool's calls in the Activity tab. */
export function initStats({ filter }) {
  onFilter = filter;
  const body = document.querySelector("#pertool tbody");
  const pick = (ev) => { const tr = ev.target.closest("tr[data-name]"); if (tr) onFilter(tr.dataset.name); };
  body.addEventListener("click", pick);
  body.addEventListener("keydown", (k) => { if (k.key === "Enter") pick(k); });
  for (const th of document.querySelectorAll("#pertool th.sortable")) {
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      S.sort = S.sort.key === k ? { key: k, dir: -S.sort.dir } : { key: k, dir: k === "name" ? 1 : -1 };
      if (S.stats) renderPerTool();
    });
  }
  for (const b of document.querySelectorAll("#range button")) {
    b.addEventListener("click", () => {
      S.range = Number(b.dataset.s);
      for (const o of document.querySelectorAll("#range button")) o.setAttribute("aria-pressed", String(o === b));
      $("range-label").textContent = RANGE_LABEL[S.range];
      load();
    });
  }
  window.addEventListener("resize", debounce(renderChart, 150));
}
