// The one live connection: an EventSource that is closed while the page is hidden.
import { apiUrl } from "./api.js";

const TYPES = ["audit", "process", "tasks", "log"];

/**
 * `handlers` maps a message type to a function taking the parsed data. `onState(state)` gets
 * "connecting", "live" or "down". `onResume()` runs on every open after the first, so views
 * can fetch what they missed while disconnected or hidden.
 */
export function startLive(handlers, { onState, onResume }) {
  let es = null;
  let opened = false;
  let backoff = 1000;
  let retry = null;

  function connect() {
    if (es || document.hidden) return;
    onState("connecting");
    es = new EventSource(apiUrl("stream"));
    for (const type of TYPES) {
      es.addEventListener(type, (m) => {
        let data;
        try { data = JSON.parse(m.data); } catch (_) { return; }
        handlers[type](data);
      });
    }
    es.onopen = () => {
      backoff = 1000;
      onState("live");
      if (opened) onResume();
      opened = true;
    };
    es.onerror = () => {
      onState("down");
      if (es && es.readyState === EventSource.CLOSED) {
        // The browser gave up (e.g. a non-200 reply); retry ourselves with backoff.
        es.close();
        es = null;
        retry = setTimeout(() => { retry = null; connect(); }, backoff);
        backoff = Math.min(backoff * 2, 30000);
      }
    };
  }

  function disconnect() {
    if (retry) { clearTimeout(retry); retry = null; }
    if (es) { es.close(); es = null; }
  }

  // A hidden tab keeps no connection and runs no timers; it catches up when shown again.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) disconnect(); else connect();
  });
  connect();
}
