import asyncio
import json

from conftest import text

from dev_link_mcp.tools.base import NoSettings
from dev_link_mcp.tools.tasks import TasksGroup


def _store(rt):
    return rt.config.state_dir / "tasks.json"


async def test_task_add_and_list_tree(connect, rt):
    async with connect("tasks") as client:
        r = await client.call_tool("task_add", {"title": "Build feature", "details": "line one\nline two"})
        assert not r.is_error
        assert "added task 1" in text(r)
        r = await client.call_tool("task_add", {"title": "Write tests", "parent_id": 1})
        assert "added task 2 under task 1" in text(r)
        await client.call_tool("task_add", {"title": "Ship"})
        out = text(await client.call_tool("task_list", {}))
    lines = out.splitlines()
    assert lines[0] == "3 tasks: 3 todo, 0 in_progress, 0 done, 0 blocked"
    assert "[1] todo: Build feature" in lines
    assert "    line one" in lines and "    line two" in lines
    assert "  [2] todo: Write tests" in lines
    # Subtask follows its parent, before the next top-level task.
    assert lines.index("  [2] todo: Write tests") < lines.index("[3] todo: Ship")


async def test_task_add_stores_state_outside_workspace(connect, rt, workspace):
    async with connect("tasks") as client:
        await client.call_tool("task_add", {"title": "x"})
    data = json.loads(_store(rt).read_text())
    assert data["next_id"] == 2
    assert data["tasks"][0]["title"] == "x"
    assert data["tasks"][0]["status"] == "todo"
    assert not any(workspace.iterdir())
    assert not list(rt.config.state_dir.glob(".tasks-*"))


async def test_task_add_rejects_bad_input(connect):
    async with connect("tasks") as client:
        r = await client.call_tool("task_add", {"title": "   "})
        assert r.is_error and "must not be empty" in text(r)
        r = await client.call_tool("task_add", {"title": "a" * 501})
        assert r.is_error and "under 500" in text(r)
        r = await client.call_tool("task_add", {"title": "child", "parent_id": 42})
        assert r.is_error and "no task with id 42" in text(r)
        r = await client.call_tool("task_list", {})
    assert text(r) == "no tasks yet; add one with task_add"


async def test_task_title_whitespace_is_normalized(connect):
    async with connect("tasks") as client:
        r = await client.call_tool("task_add", {"title": "  multi\n  line   title "})
    assert text(r) == "added task 1: multi line title"


async def test_task_update_status_title_and_notes(connect, rt):
    async with connect("tasks") as client:
        await client.call_tool("task_add", {"title": "Old"})
        r = await client.call_tool("task_update", {"id": 1, "status": "in_progress", "title": "New"})
        assert not r.is_error
        assert "status todo -> in_progress" in text(r) and "title changed" in text(r)
        await client.call_tool("task_update", {"id": 1, "notes": "first finding"})
        r = await client.call_tool("task_update", {"id": 1, "notes": "second\nmultiline", "status": "done"})
        assert "note added" in text(r)
        out = text(await client.call_tool("task_list", {}))
    assert "[1] done: New" in out
    assert out.index("first finding") < out.index("second")
    assert "      multiline" in out
    task = json.loads(_store(rt).read_text())["tasks"][0]
    assert [n["text"] for n in task["notes"]] == ["first finding", "second\nmultiline"]


async def test_task_update_errors(connect):
    async with connect("tasks") as client:
        await client.call_tool("task_add", {"title": "t"})
        r = await client.call_tool("task_update", {"id": 1})
        assert r.is_error and "nothing to update" in text(r)
        r = await client.call_tool("task_update", {"id": 1, "notes": "   "})
        assert r.is_error and "nothing to update" in text(r)
        r = await client.call_tool("task_update", {"id": 9, "status": "done"})
        assert r.is_error and "no task with id 9" in text(r)
        r = await client.call_tool("task_update", {"id": 1, "status": "finished"})
        assert r.is_error
        r = await client.call_tool("task_update", {"id": 1, "title": ""})
        assert r.is_error and "must not be empty" in text(r)


async def test_task_update_noop_does_not_write(connect, rt):
    async with connect("tasks") as client:
        await client.call_tool("task_add", {"title": "t"})
        before = _store(rt).stat().st_mtime_ns
        r = await client.call_tool("task_update", {"id": 1, "status": "todo"})
    assert not r.is_error and "no change" in text(r)
    assert _store(rt).stat().st_mtime_ns == before


async def test_task_list_status_filter(connect):
    async with connect("tasks") as client:
        await client.call_tool("task_add", {"title": "parent"})
        await client.call_tool("task_add", {"title": "child", "parent_id": 1})
        await client.call_tool("task_update", {"id": 2, "status": "blocked"})
        out = text(await client.call_tool("task_list", {"status": "blocked"}))
        assert "[2] blocked: child (parent 1)" in out
        assert "parent" not in out.replace("(parent 1)", "")
        out = text(await client.call_tool("task_list", {"status": "done"}))
        assert "(no tasks with status done)" in out
        r = await client.call_tool("task_list", {"status": "bogus"})
    assert r.is_error


async def test_task_list_is_clipped(connect, rt):
    rt.config.limits.max_output_bytes = 300
    async with connect("tasks") as client:
        for i in range(30):
            await client.call_tool("task_add", {"title": f"task number {i}"})
        out = text(await client.call_tool("task_list", {}))
    assert "characters elided" in out and "pass a status filter" in out


async def test_concurrent_adds_do_not_lose_updates(connect, rt):
    async with connect("tasks") as client:
        await asyncio.gather(*(client.call_tool("task_add", {"title": f"t{i}"}) for i in range(20)))
    data = json.loads(_store(rt).read_text())
    assert sorted(t["id"] for t in data["tasks"]) == list(range(1, 21))
    assert data["next_id"] == 21


async def test_state_persists_across_servers(connect):
    async with connect("tasks") as client:
        await client.call_tool("task_add", {"title": "persisted"})
        await client.call_tool("note_write", {"key": "k", "content": "v"})
    async with connect("tasks") as client:
        assert "persisted" in text(await client.call_tool("task_list", {}))
        assert text(await client.call_tool("note_read", {"key": "k"})) == "v"


async def test_notes_crud(connect):
    async with connect("tasks") as client:
        assert text(await client.call_tool("note_list", {})) == "no notes yet; save one with note_write"
        r = await client.call_tool("note_write", {"key": "design", "content": "use sqlite\n"})
        assert not r.is_error and "created note 'design' (11 bytes)" in text(r)
        r = await client.call_tool("note_write", {"key": "design", "content": "use json"})
        assert "updated note 'design'" in text(r)
        await client.call_tool("note_write", {"key": "a/findings", "content": "héllo"})
        assert text(await client.call_tool("note_read", {"key": " design "})) == "use json"
        listing = text(await client.call_tool("note_list", {})).splitlines()
        assert listing[0].startswith("a/findings  (") and listing[0].endswith(", 6 bytes)")
        assert listing[1].startswith("design  (")
        r = await client.call_tool("note_delete", {"key": "design"})
        assert not r.is_error and "deleted note 'design'" in text(r)
        r = await client.call_tool("note_read", {"key": "design"})
        assert r.is_error and "existing notes: a/findings" in text(r)
        r = await client.call_tool("note_delete", {"key": "design"})
    assert r.is_error and "no note 'design'" in text(r)


async def test_note_key_and_size_validation(connect, rt):
    rt.config.limits.max_output_bytes = 10
    async with connect("tasks") as client:
        r = await client.call_tool("note_write", {"key": "", "content": "x"})
        assert r.is_error and "must not be empty" in text(r)
        r = await client.call_tool("note_write", {"key": "a\nb", "content": "x"})
        assert r.is_error and "control characters" in text(r)
        r = await client.call_tool("note_write", {"key": "k" * 201, "content": "x"})
        assert r.is_error and "limit is 200" in text(r)
        r = await client.call_tool("note_write", {"key": "big", "content": "x" * 11})
        assert r.is_error and "limit is 10" in text(r)
        r = await client.call_tool("note_write", {"key": "ok", "content": "x" * 10})
    assert not r.is_error


async def test_note_at_size_limit_reads_back_whole(connect, rt):
    rt.config.limits.max_output_bytes = 1000
    async with connect("tasks") as client:
        assert not (await client.call_tool("note_write", {"key": "long", "content": "y" * 1000})).is_error
        assert text(await client.call_tool("note_read", {"key": "long"})) == "y" * 1000


async def test_note_read_is_clipped_after_limit_lowered(connect, rt):
    async with connect("tasks") as client:
        await client.call_tool("note_write", {"key": "long", "content": "y" * 1000})
    rt.config.limits.max_output_bytes = 100
    async with connect("tasks") as client:
        out = text(await client.call_tool("note_read", {"key": "long"}))
    assert "characters elided" in out and len(out) < 200


async def test_task_text_size_limits(connect, rt):
    rt.config.limits.max_output_bytes = 50
    async with connect("tasks") as client:
        r = await client.call_tool("task_add", {"title": "t", "details": "é" * 26})
        assert r.is_error and "details is 52 bytes; the limit is 50" in text(r)
        assert not (await client.call_tool("task_add", {"title": "t", "details": "d" * 50})).is_error
        r = await client.call_tool("task_update", {"id": 1, "notes": "n" * 51})
        assert r.is_error and "notes is 51 bytes" in text(r)
    assert json.loads(_store(rt).read_text())["tasks"][0]["notes"] == []


async def test_deeply_nested_tasks_list(connect, rt):
    # Deeper than the default recursion limit; the tree must still render.
    tasks = [
        {
            "id": i,
            "title": f"t{i}",
            "details": "",
            "status": "todo",
            "parent_id": i - 1 or None,
            "notes": [],
            "created": "c",
            "updated": "u",
        }
        for i in range(1, 1501)
    ]
    _store(rt).write_text(json.dumps({"next_id": 1501, "tasks": tasks, "notes": {}}))
    rt.config.limits.max_output_bytes = 10_000_000
    async with connect("tasks") as client:
        r = await client.call_tool("task_list", {})
    assert not r.is_error
    lines = text(r).splitlines()
    assert lines[2] == "[1] todo: t1"
    assert lines[-1] == "  " * 1499 + "[1500] todo: t1500"


async def test_corrupt_store_reports_error(connect, rt):
    _store(rt).write_text("{not json")
    async with connect("tasks") as client:
        r = await client.call_tool("task_list", {})
        assert r.is_error and "not valid JSON" in text(r)
        r = await client.call_tool("task_add", {"title": "x"})
        assert r.is_error
    # The broken file is left for a human to inspect, not overwritten.
    assert _store(rt).read_text() == "{not json"
    snap = TasksGroup(rt, NoSettings()).snapshot()
    assert snap["tasks"] == [] and snap["notes"] == [] and "not valid JSON" in snap["error"]

    _store(rt).write_text("[]")
    async with connect("tasks") as client:
        r = await client.call_tool("note_list", {})
    assert r.is_error and "unexpected layout" in text(r)


async def test_tasks_snapshot(connect, rt):
    assert TasksGroup(rt, NoSettings()).snapshot() == {"tasks": [], "notes": []}
    async with connect("tasks") as client:
        await client.call_tool("task_add", {"title": "one", "details": "d"})
        await client.call_tool("note_write", {"key": "b", "content": "1234"})
        await client.call_tool("note_write", {"key": "a", "content": ""})
    snap = TasksGroup(rt, NoSettings()).snapshot()
    assert [t["title"] for t in snap["tasks"]] == ["one"]
    assert snap["tasks"][0]["details"] == "d"
    assert [(n["key"], n["size"]) for n in snap["notes"]] == [("a", 0), ("b", 4)]
    assert all(n["updated"].endswith("Z") for n in snap["notes"])


async def test_annotations(connect):
    async with connect("tasks") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "task_add",
        "task_update",
        "task_list",
        "note_write",
        "note_read",
        "note_list",
        "note_delete",
    }
    for name in ("task_list", "note_read", "note_list"):
        assert tools[name].annotations.read_only_hint is True
    assert tools["note_delete"].annotations.destructive_hint is True
    assert tools["task_add"].annotations.destructive_hint is False


async def test_tool_calls_are_audited(connect, rt):
    async with connect("tasks") as client:
        await client.call_tool("task_add", {"title": "audited"})
    assert [e["name"] for e in rt.audit.query(kind="tool")] == ["task_add"]
