// Tool catalog: groups and tools from the server, with parameters, annotations and call stats.
import { getJSON } from "./api.js";
import { $, h } from "./dom.js";
import { fmtInt, fmtMs } from "./fmt.js";
import { debounce, showError } from "./ui.js";

const T = { data: null, loading: null, sel: null, stats: null };
let onFilter = () => {};

export function load() {
  if (!T.loading) {
    T.loading = getJSON("tools").then((d) => { T.data = d; render(); }).catch((e) => {
      T.loading = null;
      showError("Could not load tools: " + e.message);
    });
  }
  return T.loading;
}

/** New per-tool stats for the selected range. */
export function setStats(stats) {
  T.stats = stats;
  if (T.data && T.sel) renderDetail();
}

function find(name) {
  for (const g of T.data.groups) for (const t of g.tools) if (t.name === name) return [g.name, t];
  return [null, null];
}

function matchesQuery(q, group, t) {
  return !q || t.name.toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q) || group.includes(q);
}

function render() {
  if (!T.data) return;
  const q = $("tool-q").value.trim().toLowerCase();
  const box = $("tool-list");
  const out = [];
  let shown = 0, total = 0;
  for (const g of T.data.groups) {
    total += g.tools.length;
    const hits = g.tools.filter((t) => matchesQuery(q, g.name, t));
    if (!hits.length) continue;
    shown += hits.length;
    out.push(h("h4", null, h("span", null, g.name), h("span", { class: "muted" }, String(hits.length))));
    for (const t of hits) {
      out.push(h("button", { type: "button", "data-tool": t.name, "aria-current": String(t.name === T.sel), title: t.title || t.name }, t.name));
    }
  }
  const unavailable = Object.entries(T.data.unavailable || {}).filter(([g]) => !q || g.includes(q));
  for (const [g, hint] of unavailable) {
    out.push(h("h4", null, h("span", null, g), h("span", { class: "muted" }, "not installed")));
    out.push(h("div", { class: "group-note" }, "Install with: ", h("span", { class: "mono" }, hint)));
  }
  const disabled = (T.data.disabled || []).filter((g) => !q || g.includes(q));
  if (disabled.length) {
    out.push(h("h4", null, h("span", null, "disabled"), h("span", { class: "muted" }, String(disabled.length))));
    out.push(h("div", { class: "group-note" }, disabled.join(", "), " (turn on with --enable or tools.enabled)"));
  }
  box.replaceChildren(...(out.length ? out : [h("div", { class: "empty-state" }, "No tools match.")]));
  $("tool-match").textContent = q ? shown + " of " + total + " tools" : total + " tools in " + T.data.groups.length + " groups";
  if (!T.sel && T.data.groups.length && T.data.groups[0].tools.length) select(T.data.groups[0].tools[0].name);
}

function select(name) {
  T.sel = name;
  for (const b of $("tool-list").querySelectorAll("button[data-tool]")) b.setAttribute("aria-current", String(b.dataset.tool === name));
  renderDetail();
}

function typeOf(schema) {
  if (!schema || typeof schema !== "object") return "";
  if (schema.type) return Array.isArray(schema.type) ? schema.type.join(" | ") : schema.type + (schema.items && schema.items.type ? "[" + schema.items.type + "]" : "");
  const alts = schema.anyOf || schema.oneOf;
  if (Array.isArray(alts)) return alts.map(typeOf).filter(Boolean).join(" | ");
  if (schema.$ref) return String(schema.$ref).split("/").pop();
  if (schema.enum) return "enum";
  return "";
}

function enumOf(schema) {
  if (!schema) return null;
  if (Array.isArray(schema.enum)) return schema.enum;
  for (const alt of schema.anyOf || schema.oneOf || []) { const e = enumOf(alt); if (e) return e; }
  return null;
}

function paramsTable(schema) {
  const props = (schema && schema.properties) || {};
  const names = Object.keys(props);
  if (!names.length) return h("p", { class: "muted small" }, "No parameters.");
  const required = new Set((schema && schema.required) || []);
  return h("div", { class: "scroll" }, h("table", { class: "ptable" },
    h("thead", null, h("tr", null, h("th", null, "Name"), h("th", null, "Type"), h("th", null, "Required"), h("th", null, "Default"), h("th", null, "Description"))),
    h("tbody", null, names.map((n) => {
      const p = props[n] || {};
      const values = enumOf(p);
      return h("tr", null,
        h("td", { class: "mono" }, n),
        h("td", { class: "mono" }, typeOf(p)),
        h("td", null, required.has(n) ? "yes" : ""),
        h("td", { class: "mono wrap-cell" }, "default" in p ? JSON.stringify(p.default) : ""),
        h("td", { class: "desc-cell" }, p.description || p.title || "",
          values ? h("div", { class: "mono small" }, "one of: " + values.map((v) => JSON.stringify(v)).join(", ")) : null));
    }))));
}

function badges(a) {
  const out = [];
  const add = (on, label, cls) => { if (on) out.push(h("span", { class: "badge " + (cls || "") }, label)); };
  add(a.read_only === true, "read-only", "good");
  add(a.read_only === false, "writes");
  add(a.destructive === true, "destructive", "warn");
  add(a.idempotent === true, "idempotent");
  add(a.open_world === true, "open world", "warn");
  return out.length ? h("div", { class: "badges" }, out) : h("p", { class: "muted small" }, "No annotations.");
}

function renderDetail() {
  const [group, t] = find(T.sel);
  const box = $("tool-detail");
  if (!t) { box.replaceChildren(h("div", { class: "empty-state" }, "Select a tool.")); return; }
  const st = ((T.stats && T.stats.per_tool) || []).find((r) => r.name === t.name);
  const stats = st
    ? h("div", { class: "statline" },
      h("span", null, h("b", null, fmtInt(st.calls)), " calls"),
      h("span", { class: st.errors ? "err-text" : "" }, h("b", null, fmtInt(st.errors)), " errors"),
      h("span", null, "avg ", h("b", null, fmtMs(st.avg_ms))),
      h("span", null, "max ", h("b", null, fmtMs(st.max_ms))),
      h("button", { type: "button", class: "btn small", onclick: () => onFilter(t.name) }, "Show calls in Activity"))
    : h("div", { class: "statline" }, h("span", { class: "muted" }, "No calls in the selected range."),
      h("button", { type: "button", class: "btn small", onclick: () => onFilter(t.name) }, "Show calls in Activity"));
  box.replaceChildren(
    h("h3", null, t.name),
    h("div", { class: "muted small" }, "group " + group + (t.title && t.title !== t.name ? " · " + t.title : "")),
    h("div", { class: "desc" }, (t.description || "").trim() || "No description."),
    h("h4", null, "Annotations"), badges(t.annotations || {}),
    h("h4", null, "Parameters"), paramsTable(t.input_schema),
    h("h4", null, "Calls in the selected range"), stats,
  );
}

/** Wire the Tools tab. `filter(name)` shows one tool's calls in the Activity tab. */
export function initTools({ filter }) {
  onFilter = filter;
  $("tool-q").addEventListener("input", debounce(render, 120));
  $("tool-list").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-tool]");
    if (b) select(b.dataset.tool);
  });
}
