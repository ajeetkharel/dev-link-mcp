import { $, h } from "./dom.js";
import { fmtMs } from "./fmt.js";

export function pill(status) {
  const cls = status === "ok" || status === "error" || status === "running" ? status : "other";
  return h("span", { class: "pill " + cls }, status || "?");
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/** Run `fn` at most once per `ms`, trailing: calls inside the window collapse into one. */
export function throttle(fn, ms) {
  let t = null;
  return () => {
    if (t !== null) return;
    t = setTimeout(() => { t = null; fn(); }, ms);
  };
}

export function showError(msg) {
  const b = $("banner");
  if (!msg) { b.hidden = true; return; }
  b.textContent = msg;
  b.hidden = false;
}

export async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    // Plain-http dashboards (LAN, tunnels without TLS) have no clipboard API.
    const ta = h("textarea");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  const old = btn.textContent;
  btn.textContent = "Copied";
  setTimeout(() => { btn.textContent = old; }, 1200);
}

// The one periodic timer: it ticks elapsed times of running calls and process uptimes, and only
// while such elements exist and the page is visible.
let tickTimer = null;

function tick() {
  const nodes = document.querySelectorAll("[data-since]");
  if (!nodes.length || document.hidden) { clearInterval(tickTimer); tickTimer = null; return; }
  const now = Date.now();
  for (const el of nodes) el.textContent = fmtMs(now - Number(el.dataset.since) * 1000);
}

/** Start the ticker if anything shows a running time. Call after rendering such elements. */
export function ensureTicker() {
  if (tickTimer === null && !document.hidden && document.querySelector("[data-since]")) {
    tickTimer = setInterval(tick, 1000);
  }
}

/** A running duration: text that the ticker keeps current. `since` is in seconds. */
export function elapsed(since) {
  return h("span", { class: "elapsed", "data-since": since }, fmtMs(Date.now() - since * 1000));
}
