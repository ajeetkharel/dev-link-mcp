import { h } from "./dom.js";

/**
 * Fixed-height virtual list: only rows inside the viewport (plus `overscan`) exist in the DOM.
 *
 * Rows are keyed, so inserting at the top only moves the rows already shown (a transform
 * change) instead of rebuilding them. `key(i)` returns a stable key for index i and
 * `render(i)` builds a new row element for it. `onPaint(first, last)` runs after each paint
 * with the visible index range.
 */
export class VirtualList {
  constructor(viewport, { rowHeight, key, render, overscan = 6, onPaint = null }) {
    this.viewport = viewport;
    this.rowHeight = rowHeight;
    this.key = key;
    this.render = render;
    this.overscan = overscan;
    this.onPaint = onPaint;
    this.count = 0;
    this.nodes = new Map();
    this.queued = false;
    this.spacer = h("div", { class: "vl-spacer" });
    viewport.append(this.spacer);
    viewport.addEventListener("scroll", () => this.schedule(), { passive: true });
    new ResizeObserver(() => this.schedule()).observe(viewport);
  }

  setCount(n) {
    if (n === this.count) return;
    this.count = n;
    this.spacer.style.height = n * this.rowHeight + "px";
    this.schedule();
  }

  schedule() {
    if (this.queued) return;
    this.queued = true;
    requestAnimationFrame(() => { this.queued = false; this.paint(); });
  }

  /** Index range [first, last) currently inside the viewport, without overscan. */
  visible() {
    const top = this.viewport.scrollTop;
    const first = Math.floor(top / this.rowHeight);
    const last = Math.min(this.count, Math.ceil((top + this.viewport.clientHeight) / this.rowHeight));
    return [first, last];
  }

  paint() {
    const [vFirst, vLast] = this.visible();
    const first = Math.max(0, vFirst - this.overscan);
    const last = Math.min(this.count, vLast + this.overscan);
    const seen = new Set();
    for (let i = first; i < last; i++) {
      const k = this.key(i);
      seen.add(k);
      let node = this.nodes.get(k);
      if (!node) {
        node = { el: this.render(i), i: -1 };
        node.el.classList.add("vl-row");
        this.spacer.append(node.el);
        this.nodes.set(k, node);
      }
      if (node.i !== i) {
        node.el.style.transform = "translateY(" + i * this.rowHeight + "px)";
        node.i = i;
      }
    }
    for (const [k, node] of this.nodes) {
      if (!seen.has(k)) { node.el.remove(); this.nodes.delete(k); }
    }
    if (this.onPaint) this.onPaint(vFirst, vLast);
  }

  /** Rebuild the row for `k` in place if it is on screen. */
  refresh(k) {
    const node = this.nodes.get(k);
    if (!node || node.i < 0) return;
    const el = this.render(node.i);
    el.classList.add("vl-row");
    el.style.transform = "translateY(" + node.i * this.rowHeight + "px)";
    node.el.replaceWith(el);
    node.el = el;
  }

  /** Drop every rendered row, e.g. after the data was replaced. */
  reset() {
    for (const node of this.nodes.values()) node.el.remove();
    this.nodes.clear();
    this.schedule();
  }

  element(k) {
    const node = this.nodes.get(k);
    return node ? node.el : null;
  }

  /** Scroll the minimum needed to show index i. */
  reveal(i) {
    const top = i * this.rowHeight, bottom = top + this.rowHeight;
    const vp = this.viewport;
    if (top < vp.scrollTop) vp.scrollTop = top;
    else if (bottom > vp.scrollTop + vp.clientHeight) vp.scrollTop = bottom - vp.clientHeight;
  }

  atBottom() {
    const vp = this.viewport;
    return vp.scrollTop + vp.clientHeight >= vp.scrollHeight - this.rowHeight;
  }
}
