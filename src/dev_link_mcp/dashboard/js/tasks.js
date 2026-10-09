// The agent's task tree and notes, re-read whenever the server reports a change.
import { getJSON } from "./api.js";
import { $, h } from "./dom.js";
import { fmtBytes } from "./fmt.js";
import { showError, throttle } from "./ui.js";

let data = { tasks: [], notes: [] };

export async function load() {
  try {
    data = await getJSON("tasks");
  } catch (e) { showError("Could not load tasks: " + e.message); return; }
  render();
}
const loadSoon = throttle(load, 300);

/** Live "tasks" message: a task or note changed. */
export function onTasks() { loadSoon(); }

function render() {
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const notes = Array.isArray(data.notes) ? data.notes : [];
  $("task-count").textContent = tasks.filter((t) => t.status !== "done").length;
  const counts = {};
  for (const t of tasks) counts[t.status] = (counts[t.status] || 0) + 1;
  $("task-summary").textContent = Object.entries(counts).map(([k, v]) => v + " " + String(k).replace("_", " ")).join(" · ");
  const ids = new Set(tasks.map((t) => t.id));
  const children = new Map();
  for (const t of tasks) {
    const parent = t.parent_id !== null && t.parent_id !== undefined && ids.has(t.parent_id) ? t.parent_id : null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(t);
  }
  const out = [];
  const seen = new Set();
  // Explicit stack: an agent can nest subtasks deeper than the JS call stack allows.
  const stack = (children.get(null) || []).slice().reverse().map((t) => [t, 0]);
  while (stack.length) {
    const [t, depth] = stack.pop();
    if (seen.has(t.id)) continue;
    seen.add(t.id);
    const tnotes = Array.isArray(t.notes) ? t.notes : [];
    const el = h("div", { class: "task " + (t.status || "") },
      h("div", { class: "row" },
        h("span", { class: "id" }, "#" + t.id),
        h("span", { class: "st " + (t.status || "") }, String(t.status || "").replace("_", " ")),
        h("span", { class: "title" }, t.title)),
      t.details ? h("div", { class: "details" }, t.details) : null,
      tnotes.map((n) => h("div", { class: "tnote" }, h("span", null, n.ts), n.text)));
    el.style.paddingLeft = 14 + Math.min(depth, 12) * 22 + "px";
    out.push(el);
    for (const c of (children.get(t.id) || []).slice().reverse()) stack.push([c, depth + 1]);
  }
  if (data.error) out.unshift(h("div", { class: "banner" }, data.error));
  $("tasks").replaceChildren(...(out.length ? out : [h("div", { class: "empty-state" }, "No tasks yet. The agent adds them with task_add.")]));
  $("notes-empty").hidden = notes.length > 0;
  $("notes").replaceChildren(...notes.map((n) => h("tr", null,
    h("td", { class: "mono" }, n.key), h("td", { class: "muted" }, n.updated), h("td", { class: "num" }, fmtBytes(n.size)))));
}

export function initTasks() {
  $("tasks-refresh").addEventListener("click", load);
}
