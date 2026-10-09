// Entry point: header, tabs, and wiring the views to the live stream.
import * as activity from "./activity.js";
import { getJSON } from "./api.js";
import { $, h } from "./dom.js";
import * as drawer from "./drawer.js";
import * as files from "./files.js";
import { startLive } from "./live.js";
import * as processes from "./processes.js";
import * as stats from "./stats.js";
import * as tasks from "./tasks.js";
import * as tools from "./tools.js";
import { ensureTicker, showError } from "./ui.js";

const TABS = ["activity", "processes", "tasks", "files", "tools"];
let tab = "activity";
let liveState = "connecting";

function setLive(state) {
  liveState = state;
  const el = $("live");
  const paused = activity.isPaused();
  const shown = state === "live" && paused ? "paused" : state;
  el.dataset.state = shown;
  el.querySelector("span").textContent = {
    connecting: "Connecting", live: "Live", paused: "Paused", down: "Reconnecting",
  }[shown];
}

async function loadInfo() {
  let info;
  try {
    info = await getJSON("info");
  } catch (e) { showError("Could not load server info: " + e.message); return; }
  $("ws").textContent = info.workspace;
  $("ws").title = info.workspace;
  const chip = (k, v, warn) => h("span", { class: "chip" + (warn ? " warn" : "") }, k + " ", h("b", null, v));
  $("chips").replaceChildren(
    chip("auth", info.auth, info.auth === "none"),
    chip("sandbox", info.sandbox, info.sandbox === "none"),
    chip("network", info.network ? "on" : "off", false),
  );
  const names = [];
  for (const list of Object.values(info.groups || {})) for (const t of list) names.push(t.name);
  names.sort();
  $("tool-count").textContent = names.length;
  activity.setToolNames(names);
}

function switchTab(name) {
  tab = name;
  for (const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const t of TABS) $("p-" + t).hidden = t !== name;
  processes.setVisible(name === "processes" && !document.hidden);
  if (name === "tasks") tasks.load();
  if (name === "files") files.show();
  if (name === "tools") tools.load();
  ensureTicker();
}

function filterActivity(key, value) {
  switchTab("activity");
  activity.setFilter(key, value);
}

function resume() {
  activity.catchUp();
  stats.load();
  processes.load();
  tasks.load();
}

activity.initActivity({ open: drawer.open });
drawer.initDrawer({ filter: filterActivity });
stats.initStats({ filter: (name) => filterActivity("name", name) });
stats.onStats(tools.setStats);
processes.initProcesses();
tasks.initTasks();
files.initFiles();
tools.initTools({ filter: (name) => filterActivity("name", name) });
for (const b of document.querySelectorAll("#tabs button")) b.addEventListener("click", () => switchTab(b.dataset.tab));
document.addEventListener("live-paused", () => setLive(liveState));
document.addEventListener("visibilitychange", () => {
  processes.setVisible(tab === "processes" && !document.hidden);
  if (!document.hidden) ensureTicker();
});

loadInfo();
activity.reload();
stats.load();
processes.load();
tasks.load();
startLive(
  {
    audit: (ev) => { activity.onAudit(ev); drawer.onAudit(ev); stats.onAudit(ev); },
    process: processes.onProcess,
    tasks: tasks.onTasks,
    log: processes.onLog,
  },
  { onState: setLive, onResume: resume },
);
