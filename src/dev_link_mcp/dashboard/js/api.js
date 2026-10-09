// Every URL is relative to the page, which is served at the secret UI path with a trailing slash.

export const apiUrl = (path) => "api/" + path;

async function failure(r) {
  let msg = r.status + " " + r.statusText;
  try {
    const j = await r.json();
    if (j && j.error) msg = j.error;
  } catch (_) { /* not JSON */ }
  return new Error(msg);
}

/** GET api/<path> as JSON. Throws an Error with the server's message on a non-2xx reply. */
export async function getJSON(path, init) {
  const r = await fetch(apiUrl(path), { cache: "no-store", ...init });
  if (!r.ok) throw await failure(r);
  return r.json();
}

/** POST to api/<path>, returning the JSON reply. Throws like getJSON. */
export async function postJSON(path) {
  const r = await fetch(apiUrl(path), { method: "POST", cache: "no-store" });
  if (!r.ok) throw await failure(r);
  return r.json();
}

/** GET api/<path> and return the raw Response. Throws like getJSON. */
export async function getRaw(path, init) {
  const r = await fetch(apiUrl(path), { cache: "no-store", ...init });
  if (!r.ok) throw await failure(r);
  return r;
}

export function query(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v !== null && v !== undefined) p.set(k, String(v));
  return p.toString();
}
