"""Persistent task list and notes for long agent jobs.

State lives in `<state_dir>/tasks.json`, outside the workspace, so it survives
checkpoint restores and git operations on the user's files.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import DESTRUCTIVE, READ_ONLY, WRITE, clip

Status = Literal["todo", "in_progress", "done", "blocked"]
_STATUSES: tuple[Status, ...] = ("todo", "in_progress", "done", "blocked")
_MAX_TITLE = 500
_MAX_KEY = 200

# One lock per state file. Several MCPServer instances can share a Runtime (tests,
# reloads), and they must not interleave read-modify-write cycles on the same file.
_LOCKS: dict[str, asyncio.Lock] = {}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _store_path(rt: Runtime) -> Path:
    assert rt.config.state_dir is not None
    return rt.config.state_dir / "tasks.json"


def _lock(path: Path) -> asyncio.Lock:
    return _LOCKS.setdefault(str(path), asyncio.Lock())


def _empty() -> dict[str, Any]:
    return {"next_id": 1, "tasks": [], "notes": {}}


def _load(path: Path) -> dict[str, Any]:
    """Read the store, or an empty one if the file does not exist yet.

    Raises ToolError if the file is not valid JSON of the expected shape; the file is
    left in place so a human can inspect or repair it.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"task store {path} is not valid JSON ({exc}); ask the user to repair or remove it") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("tasks"), list)
        or not isinstance(data.get("notes"), dict)
        or not isinstance(data.get("next_id"), int)
    ):
        raise ToolError(f"task store {path} has an unexpected layout; ask the user to repair or remove it")
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    """Write the store atomically: a crash leaves either the old or the new file, never half of one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tasks-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _find(data: dict[str, Any], task_id: int) -> dict[str, Any]:
    for task in data["tasks"]:
        if task["id"] == task_id:
            return task
    raise ToolError(f"no task with id {task_id}; call task_list to see existing ids")


def _clean_title(title: str) -> str:
    title = " ".join(title.split())
    if not title:
        raise ToolError("title must not be empty")
    if len(title) > _MAX_TITLE:
        raise ToolError(f"title is {len(title)} characters; keep it under {_MAX_TITLE} and put the rest in details")
    return title


def _check_size(what: str, text: str, limit: int) -> None:
    size = len(text.encode("utf-8"))
    if size > limit:
        raise ToolError(f"{what} is {size} bytes; the limit is {limit}. Split it up or store it in a workspace file")


def _clean_key(key: str) -> str:
    key = key.strip()
    if not key:
        raise ToolError("note key must not be empty")
    if len(key) > _MAX_KEY:
        raise ToolError(f"note key is {len(key)} characters; the limit is {_MAX_KEY}")
    if any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ToolError("note key must not contain control characters or newlines")
    return key


def _render_task(task: dict[str, Any], depth: int, show_parent: bool) -> list[str]:
    indent = "  " * depth
    parent = f" (parent {task['parent_id']})" if show_parent and task.get("parent_id") is not None else ""
    lines = [f"{indent}[{task['id']}] {task['status']}: {task['title']}{parent}"]
    pad = indent + "    "
    if task.get("details"):
        lines.extend(pad + line for line in task["details"].splitlines())
    for note in task.get("notes", []):
        first, *rest = note["text"].splitlines() or [""]
        lines.append(f"{pad}- {note['ts']} {first}")
        lines.extend(f"{pad}  {line}" for line in rest)
    return lines


def _render_tree(tasks: list[dict[str, Any]]) -> list[str]:
    """Tasks as an indented tree in creation order, subtasks under their parent."""
    children: dict[int | None, list[dict[str, Any]]] = {}
    for task in tasks:
        children.setdefault(task.get("parent_id"), []).append(task)
    lines: list[str] = []
    # Explicit stack instead of recursion: an agent can nest subtasks deeper than Python's recursion limit.
    stack = [(task, 0) for task in reversed(children.get(None, []))]
    while stack:
        task, depth = stack.pop()
        lines.extend(_render_task(task, depth, show_parent=False))
        stack.extend((child, depth + 1) for child in reversed(children.get(task["id"], [])))
    return lines


def _counts(tasks: list[dict[str, Any]]) -> str:
    counts = {s: 0 for s in _STATUSES}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return ", ".join(f"{n} {s}" for s, n in counts.items())


def _register(mcp: MCPServer, rt: Runtime) -> None:
    path = _store_path(rt)
    lock = _lock(path)
    max_out = rt.config.limits.max_output_bytes
    # Capped at the output limit so note_read and task_list can always return a stored text whole.
    max_text = max_out
    live = rt.audit.live

    def changed(what: Literal["task", "note"]) -> None:
        # Open dashboards re-read the snapshot when told; nothing is sent while none is open.
        if live.active:
            live.publish("tasks", {"change": what})

    @mcp.tool(annotations=WRITE)
    async def task_add(
        title: Annotated[str, Field(description="Short task title (one line).")],
        details: Annotated[str, Field(description="Optional longer description, acceptance criteria, etc.")] = "",
        parent_id: Annotated[int | None, Field(description="Id of an existing task to nest this under.")] = None,
    ) -> str:
        """Add a task (status todo) to the task list, which persists across sessions and server
        restarts. Use it to plan and track multi-step work. Returns the new task id."""
        clean = _clean_title(title)
        _check_size("details", details, max_text)
        async with lock:
            data = await asyncio.to_thread(_load, path)
            if parent_id is not None:
                _find(data, parent_id)
            task_id = data["next_id"]
            now = _now()
            data["tasks"].append(
                {
                    "id": task_id,
                    "title": clean,
                    "details": details.strip(),
                    "status": "todo",
                    "parent_id": parent_id,
                    "notes": [],
                    "created": now,
                    "updated": now,
                }
            )
            data["next_id"] = task_id + 1
            await asyncio.to_thread(_save, path, data)
        changed("task")
        under = f" under task {parent_id}" if parent_id is not None else ""
        return f"added task {task_id}{under}: {clean}"

    @mcp.tool(annotations=WRITE)
    async def task_update(
        id: Annotated[int, Field(description="Task id from task_add or task_list.")],
        status: Annotated[Status | None, Field(description="New status.")] = None,
        notes: Annotated[
            str | None, Field(description="Progress note appended (with a timestamp) to the task's notes.")
        ] = None,
        title: Annotated[str | None, Field(description="Replacement title.")] = None,
    ) -> str:
        """Change a task's status or title and/or append a timestamped progress note. Needs at
        least one of status, notes, title."""
        if status is None and title is None and (notes is None or not notes.strip()):
            raise ToolError("nothing to update: pass status, notes, or title")
        clean = _clean_title(title) if title is not None else None
        if notes is not None:
            _check_size("notes", notes, max_text)
        async with lock:
            data = await asyncio.to_thread(_load, path)
            task = _find(data, id)
            changes: list[str] = []
            if status is not None and status != task["status"]:
                changes.append(f"status {task['status']} -> {status}")
                task["status"] = status
            if clean is not None and clean != task["title"]:
                changes.append("title changed")
                task["title"] = clean
            now = _now()
            if notes is not None and notes.strip():
                task.setdefault("notes", []).append({"ts": now, "text": notes.strip()})
                changes.append("note added")
            if changes:
                task["updated"] = now
                await asyncio.to_thread(_save, path, data)
        if changes:
            changed("task")
        summary = "; ".join(changes) if changes else "no change (values already set)"
        return f"task {id} [{task['status']}] {task['title']}: {summary}"

    @mcp.tool(annotations=READ_ONLY)
    async def task_list(
        status: Annotated[Status | None, Field(description="Only show tasks with this status.")] = None,
    ) -> str:
        """Show the task list with ids, statuses, details and progress notes. Without a filter, tasks
        are shown as a tree (subtasks indented under parents); with a status filter, as a flat list."""
        async with lock:
            data = await asyncio.to_thread(_load, path)
        tasks: list[dict[str, Any]] = data["tasks"]
        if not tasks:
            return "no tasks yet; add one with task_add"
        header = f"{len(tasks)} tasks: {_counts(tasks)}"
        if status is None:
            body = _render_tree(tasks)
        else:
            body = [line for t in tasks if t["status"] == status for line in _render_task(t, 0, show_parent=True)]
            if not body:
                body = [f"(no tasks with status {status})"]
        out = "\n".join([header, "", *body])
        if len(out) > max_out:
            return clip(out, max_out) + "\n\n(list truncated; pass a status filter to see fewer tasks)"
        return out

    @mcp.tool(annotations=DESTRUCTIVE)
    async def note_write(
        key: Annotated[str, Field(description="Note name, e.g. 'design' or 'findings/auth'.")],
        content: Annotated[str, Field(description="Full note text; replaces any existing note with this key.")],
    ) -> str:
        """Save a named note (findings, decisions, plans) that persists across sessions, replacing
        any note with the same key. Notes larger than the output cap are rejected."""
        clean = _clean_key(key)
        _check_size("note", content, max_text)
        size = len(content.encode("utf-8"))
        async with lock:
            data = await asyncio.to_thread(_load, path)
            existed = clean in data["notes"]
            data["notes"][clean] = {"content": content, "updated": _now()}
            await asyncio.to_thread(_save, path, data)
        changed("note")
        return f"{'updated' if existed else 'created'} note {clean!r} ({size} bytes)"

    @mcp.tool(annotations=READ_ONLY)
    async def note_read(key: Annotated[str, Field(description="Note name.")]) -> str:
        """Return the full text of a named note."""
        clean = _clean_key(key)
        async with lock:
            data = await asyncio.to_thread(_load, path)
        note = data["notes"].get(clean)
        if note is None:
            known = ", ".join(sorted(data["notes"])) or "none"
            raise ToolError(f"no note {clean!r}; existing notes: {clip(known, 2000)}")
        return clip(note["content"], max_out)

    @mcp.tool(annotations=READ_ONLY)
    async def note_list() -> str:
        """List saved notes with last-updated time (UTC) and size in bytes."""
        async with lock:
            data = await asyncio.to_thread(_load, path)
        if not data["notes"]:
            return "no notes yet; save one with note_write"
        lines = [
            f"{key}  ({note['updated']}, {len(note['content'].encode('utf-8'))} bytes)"
            for key, note in sorted(data["notes"].items())
        ]
        return clip("\n".join(lines), max_out)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def note_delete(key: Annotated[str, Field(description="Note name.")]) -> str:
        """Delete a named note permanently."""
        clean = _clean_key(key)
        async with lock:
            data = await asyncio.to_thread(_load, path)
            if clean not in data["notes"]:
                raise ToolError(f"no note {clean!r}; call note_list to see existing keys")
            del data["notes"][clean]
            await asyncio.to_thread(_save, path, data)
        changed("note")
        return f"deleted note {clean!r}"


class TasksGroup(ToolGroup):
    """Persistent task list and notes for long agent jobs."""

    name = "tasks"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)

    def snapshot(self) -> dict[str, Any]:
        """Current tasks and note metadata for the dashboard.

        Returns {"tasks": [task dicts], "notes": [{key, updated, size}]}, plus an "error"
        string when the store cannot be read. Reads without the lock: writes replace the
        file atomically, so a reader always sees a complete version. Does blocking file IO.
        """
        try:
            data = _load(_store_path(self.rt))
        except ToolError as exc:
            return {"tasks": [], "notes": [], "error": str(exc)}
        notes = [
            {"key": key, "updated": note["updated"], "size": len(note["content"].encode("utf-8"))}
            for key, note in sorted(data["notes"].items())
        ]
        return {"tasks": data["tasks"], "notes": notes}
