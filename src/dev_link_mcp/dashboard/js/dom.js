// DOM helpers. Server data is untrusted (tool args and results come from the agent and the
// programs it runs), so it only ever reaches the page as text nodes and attribute values.

const SVGNS = "http://www.w3.org/2000/svg";

export const $ = (id) => document.getElementById(id);

function setProps(el, props) {
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
}

function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

/** Create an HTML element. `props` become attributes, `on*` functions become listeners. */
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  setProps(el, props);
  return add(el, kids);
}

/** Create an SVG element, same contract as h(). */
export function s(tag, props, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  setProps(el, props);
  return add(el, kids);
}
