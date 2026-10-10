// Event detail drawer: metadata at once, large fields in slices, image paths as thumbnails.
import { apiUrl, getJSON, query } from "./api.js";
import { $, h } from "./dom.js";
import { fmtBytes, fmtDate, fmtMs } from "./fmt.js";
import { copyText, pill } from "./ui.js";

const SLICE = 65536;
// Pretty-printing parses the whole text, so only small, complete JSON gets it.
const PRETTY_MAX = 65536;
const MAX_THUMBS = 8;
// Workspace paths of raster images, e.g. ".dev-link/artifacts/x.png" in a screenshot result.
const IMAGE_PATH = /(?:^|[\s"'`(=:[])((?:\/|\.{1,2}\/)?(?:[\w@%+~.-]+\/)*[\w@%+~-][\w@%+~.-]*\.(?:png|jpe?g|gif|webp|avif))(?=$|[\s"'`),;:\]])/gi;

let current = null; // { id, status, seq }
let seq = 0;
let returnFocus = null;
let onFilter = () => {};

function section(id, field, title, text, size, err) {
  const pre = h("pre", { class: "code" + (err ? " err" : "") });
  const body = { text, size, pretty: false };
  const sizeLabel = h("span", { class: "size" });
  const copy = h("button", { type: "button", class: "btn small" }, "Copy");
  const more = h("button", { type: "button", class: "btn small" }, "Load more");
  const download = h("a", {
    class: "btn small", href: apiUrl("events/" + id + "/result?" + query({ field, download: 1 })),
    download: "event-" + id + "-" + field + ".txt",
  }, "Download raw");
  const moreRow = h("div", { class: "sec-more" }, more, download);

  const looksJson = field === "args" || /^\s*[[{]/.test(text);
  if (looksJson && size <= PRETTY_MAX && text.length >= size) {
    try { pre.textContent = JSON.stringify(JSON.parse(text), null, 2); body.pretty = true; } catch (_) { /* not JSON */ }
  }
  if (!body.pretty) pre.append(document.createTextNode(text));

  const label = () => {
    const shown = body.text.length;
    sizeLabel.textContent = shown < body.size
      ? shown.toLocaleString() + " of " + body.size.toLocaleString() + " chars"
      : body.size.toLocaleString() + " chars";
    more.hidden = shown >= body.size;
    copy.textContent = shown < body.size ? "Copy shown" : "Copy";
  };
  label();
  copy.addEventListener("click", () => copyText(body.pretty ? pre.textContent : body.text, copy));
  more.addEventListener("click", async () => {
    more.disabled = true;
    const mine = seq;
    try {
      const part = await getJSON("events/" + id + "/result?" + query({ field, offset: body.text.length, limit: SLICE }));
      if (mine !== seq) return;
      body.text += part.text;
      body.size = part.size;
      // Appending a text node leaves what is already laid out alone.
      pre.append(document.createTextNode(part.text));
    } catch (e) {
      sizeLabel.textContent = "could not load more: " + e.message;
    }
    more.disabled = false;
    label();
  });
  return h("div", { class: "sec" },
    h("div", { class: "sec-h" }, h("h4", null, title), sizeLabel, copy),
    pre, moreRow);
}

function imagePaths(texts) {
  const found = [];
  for (const t of texts) {
    if (!t) continue;
    IMAGE_PATH.lastIndex = 0;
    let m;
    while ((m = IMAGE_PATH.exec(t)) && found.length < MAX_THUMBS) {
      if (!found.includes(m[1]) && !m[1].includes("://")) found.push(m[1]);
    }
  }
  return found;
}

function thumbs(paths) {
  if (!paths.length) return null;
  const box = h("div", { class: "thumbs" });
  for (const p of paths) {
    const src = apiUrl("files/raw?" + query({ path: p }));
    const img = h("img", { src, alt: p, loading: "lazy" });
    const btn = h("button", { type: "button", class: "thumb", title: p }, img, h("span", null, p));
    // Paths in agent output may not exist or may be outside the workspace; drop those quietly.
    img.addEventListener("error", () => { btn.remove(); if (!box.children.length) box.parentElement?.remove(); });
    btn.addEventListener("click", () => openLightbox(src, p, btn));
    box.append(btn);
  }
  return h("div", { class: "sec" }, h("div", { class: "sec-h" }, h("h4", null, "Images")), box);
}

function openLightbox(src, caption, from) {
  const dlg = $("lightbox");
  $("lb-img").src = src;
  $("lb-img").alt = caption;
  $("lb-cap").textContent = caption;
  $("lb-open").href = src;
  dlg.showModal();
  dlg.addEventListener("close", () => from.focus(), { once: true });
}

function render(e) {
  $("d-title").textContent = e.name;
  $("d-title").title = e.name;
  $("d-pill").replaceChildren(pill(e.status));
  const meta = h("dl", { class: "meta" });
  const row = (k, v) => { if (v !== null && v !== undefined && v !== "") meta.append(h("dt", null, k), h("dd", null, v)); };
  row("Event", "#" + e.id + " · " + e.kind);
  row("Started", fmtDate(e.ts));
  row("Ended", e.ended ? fmtDate(e.ended) : e.status === "running" ? "still running" : "");
  row("Duration", e.duration_ms !== null && e.duration_ms !== undefined ? fmtMs(e.duration_ms) : "");
  if (e.client) {
    meta.append(h("dt", null, "Client"), h("dd", null, e.client, " ",
      e.session ? null : h("button", { type: "button", class: "btn small", onclick: () => { close(); onFilter("client", e.client); } }, "Show only this client")));
  }
  if (e.session) {
    meta.append(h("dt", null, "Session"), h("dd", null, h("span", { class: "mono" }, e.session), " ",
      h("button", { type: "button", class: "btn small", onclick: () => { close(); onFilter("session", e.session); } }, "Show only this session")));
  }
  row("Result size", e.result_bytes !== null && e.result_bytes !== undefined ? fmtBytes(e.result_bytes) : "");
  const kids = [meta];
  const images = thumbs(imagePaths([e.args, e.result]));
  if (images) kids.push(images);
  if (e.args_size) kids.push(section(e.id, "args", "Arguments", e.args, e.args_size));
  if (e.error_size) kids.push(section(e.id, "error", "Error", e.error, e.error_size, true));
  const sameAsError = e.error_size && e.result_size === e.error_size && e.result === e.error;
  if (e.result_size && !sameAsError) kids.push(section(e.id, "result", "Result", e.result, e.result_size));
  if (e.status === "running") kids.push(h("p", { class: "muted" }, "This call is still running; the view updates when it finishes."));
  $("d-body").replaceChildren(...kids);
}

async function load(id, refresh) {
  const mine = ++seq;
  if (!refresh) {
    $("d-title").textContent = "#" + id;
    $("d-pill").replaceChildren();
    $("d-body").replaceChildren(h("p", { class: "muted" }, "Loading…"));
  }
  let e;
  try {
    e = await getJSON("events/" + encodeURIComponent(id));
  } catch (err) {
    if (mine === seq) $("d-body").replaceChildren(h("p", { class: "muted" }, "Could not load event: " + err.message));
    return;
  }
  if (mine !== seq || !current || current.id !== id) return;
  current.status = e.status;
  render(e);
}

/** Show event `id` in the drawer. */
export function open(id) {
  if (current === null) returnFocus = document.activeElement;
  current = { id, status: null };
  $("drawer").hidden = false;
  $("scrim").hidden = false;
  $("d-close").focus();
  load(id, false);
}

export function close() {
  if (current === null) return;
  current = null;
  seq++;
  $("drawer").hidden = true;
  $("scrim").hidden = true;
  // The feed can run past the bottom of the window; a plain focus() would scroll the page to it.
  if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
  returnFocus = null;
}

/** Live audit message: reload only when the event on show finishes. */
export function onAudit(ev) {
  if (current && current.id === ev.id && current.status === "running" && ev.status !== "running") {
    current.status = ev.status;
    load(ev.id, true);
  }
}

/** Wire the drawer. `filter(key, value)` narrows the Activity view. */
export function initDrawer({ filter }) {
  onFilter = filter;
  $("d-close").addEventListener("click", close);
  $("scrim").addEventListener("click", close);
  $("lb-close").addEventListener("click", () => $("lightbox").close());
  $("lightbox").addEventListener("click", (ev) => { if (ev.target === $("lightbox")) $("lightbox").close(); });
  document.addEventListener("keydown", (k) => {
    // The lightbox is a modal dialog and closes itself on Escape.
    if (k.key === "Escape" && current !== null && !$("lightbox").open) close();
  });
}
