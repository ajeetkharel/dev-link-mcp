// Files tab: a lazy, read-only tree of the workspace with text, image and download previews.
import { apiUrl, getJSON, getRaw, query } from "./api.js";
import { $, h } from "./dom.js";
import { fmtBytes, fmtDate } from "./fmt.js";
import { showError } from "./ui.js";
import { VirtualList } from "./vlist.js";

const LINE = 18;
const CHUNK = 262144;
const MAX_LINE_CHARS = 4000;

const F = {
  nodes: new Map(), // path -> { path, name, dir, li, button, ul, loaded }
  selected: null,
  seq: 0,
  blobUrl: null,
  lines: [],
  partial: "",
  next: 0,
  size: 0,
  path: null,
  list: null,
  started: false,
};

const join = (dir, name) => (dir === "." ? name : dir + "/" + name);
const parentOf = (path) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ".");

function isDirEntry(e) {
  return e.type === "dir" || (e.type === "symlink" && e.followable && e.target === "dir");
}

function nodeFor(path, name, entry) {
  const dir = entry ? isDirEntry(entry) : true;
  const blocked = entry && entry.type === "symlink" && !entry.followable;
  const icon = h("span", { class: "ico", "aria-hidden": "true" }, dir ? "▸" : entry && entry.type === "symlink" ? "↪" : "·");
  const button = h("button", {
    type: "button", role: "treeitem", "data-path": path, disabled: blocked || entry?.type === "other",
    title: blocked ? "symlink pointing outside the workspace" : path,
  }, icon, h("span", null, name));
  const li = h("li", null, button);
  const node = { path, name, dir, li, button, icon, ul: null, loaded: false, open: false };
  F.nodes.set(path, node);
  return node;
}

async function loadChildren(node) {
  let listing;
  try {
    listing = await getJSON("files?" + query({ path: node.path }));
  } catch (e) { showError("Could not list " + node.path + ": " + e.message); return null; }
  const ul = h("ul", { role: "group" });
  for (const e of listing.entries) {
    const child = nodeFor(join(node.path, e.name), e.name, e);
    ul.append(child.li);
  }
  if (!listing.entries.length) ul.append(h("li", { class: "note" }, "empty"));
  if (listing.truncated) ul.append(h("li", { class: "note" }, "showing " + listing.entries.length + " of " + listing.total));
  if (node.ul) node.ul.replaceWith(ul); else node.li.append(ul);
  node.ul = ul;
  node.loaded = true;
  return listing;
}

async function setOpen(node, open) {
  if (open && !node.loaded) await loadChildren(node);
  node.open = open;
  if (node.ul) node.ul.hidden = !open;
  node.icon.textContent = open ? "▾" : "▸";
  node.button.setAttribute("aria-expanded", String(open));
}

function mark(path) {
  if (F.selected) F.nodes.get(F.selected)?.button.removeAttribute("aria-current");
  F.selected = path;
  F.nodes.get(path)?.button.setAttribute("aria-current", "true");
}

function crumbs(path, isDir) {
  const box = $("crumbs");
  const parts = path === "." ? [] : path.split("/");
  const out = [h("button", { type: "button", "data-dir": "." }, "workspace")];
  parts.forEach((p, i) => {
    out.push(h("span", null, "/"));
    const sub = parts.slice(0, i + 1).join("/");
    out.push(i === parts.length - 1 && !isDir ? h("span", null, p) : h("button", { type: "button", "data-dir": sub }, p));
  });
  box.replaceChildren(...out);
}

function resetView() {
  if (F.blobUrl) { URL.revokeObjectURL(F.blobUrl); F.blobUrl = null; }
  F.lines = [];
  F.partial = "";
  F.list = null;
}

function download(path) {
  const a = $("file-download");
  if (path === null) { a.hidden = true; return; }
  a.hidden = false;
  a.href = apiUrl("files/raw?" + query({ path, download: 1 }));
  a.setAttribute("download", path.split("/").pop());
}

async function showDir(path) {
  const mine = ++F.seq;
  resetView();
  F.path = path;
  crumbs(path, true);
  download(null);
  $("file-meta").textContent = "";
  let listing;
  try {
    listing = await getJSON("files?" + query({ path }));
  } catch (e) {
    if (mine === F.seq) $("file-view").replaceChildren(h("div", { class: "info" }, "Could not list: " + e.message));
    return;
  }
  if (mine !== F.seq) return;
  $("file-meta").textContent = listing.total + " entries" + (listing.truncated ? " (first " + listing.entries.length + " shown)" : "");
  const rows = listing.entries.map((e) => h("tr", { class: "click", tabindex: "0", "data-open": join(path, e.name), "data-dir": String(isDirEntry(e)) },
    h("td", { class: "mono" }, e.name + (isDirEntry(e) ? "/" : "")),
    h("td", { class: "muted" }, e.type === "symlink" ? (e.followable ? "symlink" : "symlink (outside)") : e.type),
    h("td", { class: "num" }, isDirEntry(e) ? "" : fmtBytes(e.size)),
    h("td", { class: "muted" }, fmtDate(e.mtime))));
  $("file-view").replaceChildren(h("div", { class: "scroll" }, h("table", null,
    h("thead", null, h("tr", null, h("th", null, "Name"), h("th", null, "Type"), h("th", { class: "num" }, "Size"), h("th", null, "Modified"))),
    h("tbody", null, rows))));
}

function addText(text, final) {
  const parts = (F.partial + text).split("\n");
  F.partial = parts.pop();
  if (final && F.partial) { parts.push(F.partial); F.partial = ""; }
  for (const p of parts) F.lines.push(p.length > MAX_LINE_CHARS ? p.slice(0, MAX_LINE_CHARS) + " …" : p);
}

function textView() {
  const box = h("div", { class: "vl lines", tabindex: "0", "aria-label": "File contents" });
  const width = String(F.lines.length + 1).length;
  F.list = new VirtualList(box, {
    rowHeight: LINE, key: (i) => i, overscan: 12,
    render: (i) => h("div", null, h("span", { class: "no" }, String(i + 1).padStart(width, " ")), F.lines[i]),
  });
  F.list.setCount(F.lines.length);
  return box;
}

function metaText() {
  const shown = F.next < F.size ? fmtBytes(F.next) + " of " : "";
  $("file-meta").textContent = shown + fmtBytes(F.size) + " · " + F.lines.length.toLocaleString() + " lines";
}

async function loadMore(btn) {
  const mine = F.seq;
  btn.disabled = true;
  try {
    const r = await getRaw("files/raw?" + query({ path: F.path, offset: F.next, limit: CHUNK }));
    const text = await r.text();
    if (mine !== F.seq) return;
    F.next = Number(r.headers.get("X-Next-Offset"));
    F.size = Number(r.headers.get("X-File-Size"));
    addText(text, F.next >= F.size);
    F.list.setCount(F.lines.length);
    metaText();
    btn.parentElement.hidden = F.next >= F.size;
  } catch (e) { showError("Could not read more: " + e.message); }
  btn.disabled = false;
}

async function showFile(path) {
  const mine = ++F.seq;
  resetView();
  F.path = path;
  crumbs(path, false);
  download(path);
  $("file-meta").textContent = "Loading…";
  const view = $("file-view");
  let r;
  try {
    r = await getRaw("files/raw?" + query({ path, limit: CHUNK }));
  } catch (e) {
    if (mine === F.seq) { $("file-meta").textContent = ""; view.replaceChildren(h("div", { class: "info" }, "Could not open: " + e.message)); }
    return;
  }
  if (mine !== F.seq) { r.body?.cancel(); return; }
  const kind = r.headers.get("X-File-Kind");
  F.size = Number(r.headers.get("X-File-Size"));
  if (kind === "image") {
    const blob = await r.blob();
    if (mine !== F.seq) return;
    F.blobUrl = URL.createObjectURL(blob);
    $("file-meta").textContent = fmtBytes(F.size) + " · " + blob.type;
    view.replaceChildren(h("div", { class: "img-box" }, h("img", { src: F.blobUrl, alt: path })));
    return;
  }
  if (kind !== "text") {
    r.body?.cancel();
    $("file-meta").textContent = fmtBytes(F.size);
    view.replaceChildren(h("div", { class: "info" }, "Binary file, " + fmtBytes(F.size) + ". No preview.",
      h("a", { class: "btn small", href: apiUrl("files/raw?" + query({ path, download: 1 })), download: path.split("/").pop() }, "Download")));
    return;
  }
  const text = await r.text();
  if (mine !== F.seq) return;
  F.next = Number(r.headers.get("X-Next-Offset"));
  addText(text, F.next >= F.size);
  const more = h("button", { type: "button", class: "btn small" }, "Load more");
  more.addEventListener("click", () => loadMore(more));
  const moreRow = h("div", { class: "sec-more info" }, more);
  moreRow.hidden = F.next >= F.size;
  view.replaceChildren(textView(), moreRow);
  metaText();
}

async function openPath(path, isDir) {
  // Open every ancestor first so the tree shows where the selection is.
  const parts = path === "." ? [] : path.split("/");
  for (let i = 0; i < parts.length - (isDir ? 0 : 1); i++) {
    const node = F.nodes.get(parts.slice(0, i + 1).join("/"));
    if (node && !node.open) await setOpen(node, true);
  }
  mark(path);
  if (isDir) await showDir(path); else await showFile(path);
}

async function refresh() {
  if (F.path === null) return;
  const node = F.nodes.get(F.path);
  const dir = node ? node.dir : F.path === ".";
  const listed = F.nodes.get(dir ? F.path : parentOf(F.path));
  if (listed && listed.loaded) await loadChildren(listed);
  await openPath(F.path, dir);
}

/** The tab was opened: build the root of the tree the first time. */
export async function show() {
  if (F.started) return;
  F.started = true;
  const root = nodeFor(".", "workspace", null);
  $("tree").replaceChildren(h("ul", null, root.li));
  await setOpen(root, true);
  await openPath(".", true);
}

export function initFiles() {
  $("tree").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button[data-path]");
    if (!b) return;
    const node = F.nodes.get(b.dataset.path);
    if (!node) return;
    if (node.dir) {
      await setOpen(node, !node.open || F.selected !== node.path);
      mark(node.path);
      await showDir(node.path);
    } else {
      await openPath(node.path, false);
    }
  });
  $("crumbs").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-dir]");
    if (b) openPath(b.dataset.dir, true);
  });
  const view = $("file-view");
  const pick = (ev) => {
    const tr = ev.target.closest("tr[data-open]");
    if (tr) openPath(tr.dataset.open, tr.dataset.dir === "true");
  };
  view.addEventListener("click", pick);
  view.addEventListener("keydown", (k) => { if (k.key === "Enter") pick(k); });
  $("files-refresh").addEventListener("click", refresh);
}
