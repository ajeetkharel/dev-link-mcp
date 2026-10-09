export const pad2 = (n) => String(n).padStart(2, "0");

/** Clock time for today, date and time otherwise. `ts` is in seconds. */
export function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const t = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  if (d.toDateString() === now.toDateString()) return t;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + t;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return "–";
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return Math.round(ms) + " ms";
  if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + " s";
  const m = Math.floor(ms / 60000), sec = Math.floor((ms % 60000) / 1000);
  if (m < 60) return m + "m " + pad2(sec) + "s";
  return Math.floor(m / 60) + "h " + pad2(m % 60) + "m";
}

export function fmtBytes(n) {
  if (n === null || n === undefined) return "–";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

export const fmtInt = (n) => (n || 0).toLocaleString();

export function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "";
}
