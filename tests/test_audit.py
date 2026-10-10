import json
import logging
import os
import sqlite3
import tempfile
import time

import pytest

from dev_link_mcp import audit as audit_module
from dev_link_mcp.audit import AuditStore, SecretLogFilter, StreamRedactor
from dev_link_mcp.config import Config
from dev_link_mcp.runtime import Runtime

TOKEN = "tok-ABCDEFGHIJKLMNOP1234"


@pytest.fixture
def store(tmp_path):
    s = AuditStore(tmp_path, secrets=[TOKEN, ""], result_bytes=1000)
    yield s
    s.close()


def _jsonl(store: AuditStore) -> list[dict]:
    return [json.loads(line) for line in store.jsonl_path.read_text().splitlines()]


def test_start_and_finish(store):
    event_id = store.start("tool", "read_file", {"path": "a.txt"}, session="s1", client="cli 1.0")
    running = store.get(event_id)
    assert running["status"] == "running" and running["ended"] is None
    assert running["args"] == {"path": "a.txt"}
    assert store.jsonl_path.read_text() == ""  # JSONL gets the finished event only
    time.sleep(0.01)
    store.finish(event_id, ok=True, result="hello")
    done = store.get(event_id)
    assert done["status"] == "ok" and done["result"] == "hello" and done["result_bytes"] == 5
    assert done["duration_ms"] > 0 and done["ended"] >= done["ts"]
    assert done["session"] == "s1" and done["client"] == "cli 1.0"
    [line] = _jsonl(store)
    assert line["id"] == event_id and line["status"] == "ok"


def test_error_and_record(store):
    event_id = store.record("tool", "boom", {"x": 1}, ok=False, error="E" * 5000)
    event = store.get(event_id)
    assert event["status"] == "error" and len(event["error"]) == 4000
    assert store.get(99999) is None


def test_record_without_args(store):
    event = store.get(store.record("server", "start"))
    assert event["args"] is None and event["status"] == "ok"


def test_token_redacted_everywhere(store, tmp_path):
    event_id = store.start(
        "tool", "run_command", {"command": f"curl -H 'x: {TOKEN}'", "nested": [{"note": f"see {TOKEN}"}]}
    )
    store.finish(event_id, ok=False, result=f"out {TOKEN}", error=f"err {TOKEN}")
    store.record("tool", "t", {"a": 1}, result=f"{TOKEN}{TOKEN}")
    event = store.get(event_id)
    assert event["args"]["command"] == "curl -H 'x: ***'"
    assert event["args"]["nested"] == [{"note": "see ***"}]
    assert event["result"] == "out ***" and event["error"] == "err ***"
    store.close()
    assert TOKEN not in (tmp_path / "audit.jsonl").read_text()
    assert TOKEN.encode() not in (tmp_path / "audit.sqlite3").read_bytes()
    db = sqlite3.connect(tmp_path / "audit.sqlite3")
    dump = "\n".join(db.iterdump())
    db.close()
    assert TOKEN not in dump


def test_secret_looking_keys_redacted(store):
    args = {
        "headers": {"Authorization": "Bearer abc", "Cookie": "sid=1", "Accept": "json"},
        "api_key": "k1",
        "API-KEY": "k2",
        "password": "p",
        "passwd": "p2",
        "client_secret": "s",
        "github_token": "g",
        "credentials": "c",
        "token": 12345,  # non-string values are kept: only strings carry secrets in practice
        "path": "x",
    }
    event = store.get(store.record("tool", "http_request", args))
    got = event["args"]
    assert got["headers"] == {"Authorization": "***", "Cookie": "***", "Accept": "json"}
    for key in ("api_key", "API-KEY", "password", "passwd", "client_secret", "github_token", "credentials"):
        assert got[key] == "***", key
    assert got["token"] == 12345 and got["path"] == "x"


def test_redact_helper(store):
    assert store.redact(f"a {TOKEN} b") == "a *** b"
    # The empty secret is ignored rather than inserting *** between every character.
    assert store.redact("abc") == "abc"


def test_stream_redactor_catches_a_secret_split_anywhere():
    # Bytes that are not UTF-8 pass through untouched.
    data = f"a{TOKEN}b{TOKEN}".encode() + b"\xff\xfe\xe2\x82 end"
    expected = b"a***b***\xff\xfe\xe2\x82 end"
    for cut in range(len(data) + 1):
        redactor = StreamRedactor([TOKEN, ""])
        assert redactor.feed(data[:cut]) + redactor.feed(data[cut:]) + redactor.flush() == expected, cut
    redactor = StreamRedactor([TOKEN])
    assert b"".join(redactor.feed(data[i : i + 1]) for i in range(len(data))) + redactor.flush() == expected


def test_stream_redactor_holds_back_only_what_could_start_a_secret():
    redactor = StreamRedactor([TOKEN])
    # A line such as a "ready" banner must not wait for more output.
    assert redactor.feed(b"ready on :3000\n") == b"ready on :3000\n"
    assert redactor.feed(b"x" + TOKEN[:5].encode()) == b"x"
    assert redactor.feed(b"!") == TOKEN[:5].encode() + b"!"
    assert redactor.feed(TOKEN[:-1].encode()) == b""
    # The end of the output is not a secret, so it is written as it is.
    assert redactor.flush() == TOKEN[:-1].encode()
    assert redactor.flush() == b""


def test_log_filter_redacts_messages_arguments_and_tracebacks(caplog):
    logger = logging.getLogger("dev_link_mcp.test_redaction")
    caplog.handler.addFilter(SecretLogFilter([TOKEN]))
    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("url /mcp/%s/x", TOKEN)
        logger.warning(f"plain {TOKEN}")
        try:
            raise ValueError(f"bad path /ui/{TOKEN}")
        except ValueError:
            logger.exception("request failed")
    assert TOKEN not in caplog.text
    assert "url /mcp/***/x" in caplog.text and "plain ***" in caplog.text
    assert "ValueError: bad path /ui/***" in caplog.text


def test_result_clipping(store):
    big = "x" * 5000
    event = store.get(store.record("tool", "big", result=big))
    assert event["result_bytes"] == 5000
    assert event["result"].startswith("x" * 1000)
    assert "clipped in audit log, 5000 bytes total" in event["result"]
    assert len(event["result"]) < 1100


def test_args_clipped_like_results(store):
    # A large tool argument (file content, process input) must not grow the store without bound.
    event_id = store.record("tool", "process_input", {"text": "y" * 5000})
    event = store.get(event_id)
    assert isinstance(event["args"], str)
    assert event["args"].startswith('{"text": "' + "y" * 990)
    assert "clipped in audit log, 5012 bytes total" in event["args"]
    assert len(event["args"]) < 1100
    assert len(_jsonl(store)[-1]["args"]) < 1100
    assert store.detail(event_id)["args_size"] < 1100


def test_small_args_stay_json(store):
    event = store.get(store.record("tool", "read_file", {"path": "a.txt"}))
    assert event["args"] == {"path": "a.txt"}


def test_result_bytes_counts_utf8(store):
    event = store.get(store.record("tool", "uni", result="é" * 10))
    assert event["result_bytes"] == 20


def test_query_filters(store):
    ids = [
        store.record("tool", "read_file", {"path": "a"}, session="s1"),
        store.record("tool", "read_file", {"path": "needle"}, ok=False, error="nope", session="s2"),
        store.record("session", "initialize", {}, session="s1"),
        store.record("tool", "grep", {"pattern": "q"}, result="needle found"),
    ]
    assert [e["id"] for e in store.query()] == ids[::-1]
    assert [e["id"] for e in store.query(kind="tool")] == [ids[3], ids[1], ids[0]]
    assert [e["id"] for e in store.query(name="read_file")] == [ids[1], ids[0]]
    assert [e["id"] for e in store.query(status="error")] == [ids[1]]
    assert [e["id"] for e in store.query(session="s1")] == [ids[2], ids[0]]
    assert [e["id"] for e in store.query(text="needle")] == [ids[3], ids[1]]
    assert [e["id"] for e in store.query(before=ids[2])] == [ids[1], ids[0]]
    assert [e["id"] for e in store.query(limit=2)] == [ids[3], ids[2]]
    assert [e["id"] for e in store.query(limit=0)] == [ids[3]]  # clamped to at least 1
    assert store.query(kind="tool", name="read_file", status="ok") == [store.query(session="s1")[1]]
    summary = store.query(limit=1)[0]
    assert "result" not in summary and "args" not in summary
    assert summary["args_preview"] == '{"pattern": "q"}'


def test_stats(store):
    store.record("tool", "a", session="s1")
    store.record("tool", "a", ok=False, error="x", session="s2")
    store.record("tool", "b")
    store.start("tool", "c")
    store.record("session", "initialize", session="s3")
    stats = store.stats()
    assert stats["calls"] == 4
    assert stats["errors"] == 1
    assert stats["running"] == 1
    assert stats["sessions"] == 3
    per_tool = {row["name"]: row for row in stats["per_tool"]}
    assert per_tool["a"]["calls"] == 2 and per_tool["a"]["errors"] == 1
    assert set(per_tool) == {"a", "b", "c"}
    assert sum(b["calls"] for b in stats["timeline"]) == 4
    assert store.stats(since_s=-10)["calls"] == 0


def test_stats_empty(store):
    stats = store.stats()
    assert stats["calls"] == 0 and stats["errors"] == 0 and stats["per_tool"] == []


async def test_subscribe_receives_events(store):
    queue = store.live.subscribe()
    event_id = store.start("tool", "x", {"a": TOKEN})
    store.finish(event_id, ok=True, result="r")
    (kind1, first), (kind2, second) = queue.get_nowait(), queue.get_nowait()
    assert kind1 == kind2 == "audit"
    assert (first["id"], first["status"]) == (event_id, "running")
    assert (second["id"], second["status"]) == (event_id, "ok")
    assert TOKEN not in first["args_preview"]
    store.live.unsubscribe(queue)
    store.record("tool", "y")
    assert queue.empty()


async def test_full_subscriber_does_not_block(store):
    queue = store.live.subscribe()
    for _ in range(600):
        store.record("tool", "spam")
    assert queue.full()
    store.record("tool", "after")  # no exception, no blocking


def test_mark_interrupted(tmp_path):
    first = AuditStore(tmp_path, secrets=[], result_bytes=100)
    running = first.start("tool", "slow")
    done = first.record("tool", "fast")
    first.close()
    second = AuditStore(tmp_path, secrets=[], result_bytes=100)
    second.mark_interrupted()
    assert second.get(running)["status"] == "error"
    assert "server stopped" in second.get(running)["error"]
    assert second.get(done)["status"] == "ok"
    # Data persists across reopen and new ids continue after old ones.
    assert second.record("tool", "next") > done
    second.close()


def test_query_after_pages_forward(store):
    ids = [store.record("tool", "a" if i % 2 else "b") for i in range(6)]
    assert [e["id"] for e in store.query(after=ids[1])] == ids[2:]
    assert [e["id"] for e in store.query(after=ids[1], limit=2)] == ids[2:4]
    assert [e["id"] for e in store.query(after=ids[0], name="a")] == [ids[1], ids[3], ids[5]]
    assert [e["id"] for e in store.query(after=ids[0], before=ids[4])] == ids[1:4]
    assert [e["id"] for e in store.query(after=0, limit=1)] == [ids[0]]
    assert store.query(after=ids[-1]) == []


def test_query_client_filter(store):
    a = store.record("tool", "x", client="one 1")
    store.record("tool", "x", client="two 2")
    assert [e["id"] for e in store.query(client="one 1")] == [a]


def test_detail_and_slices(store):
    event_id = store.record("tool", "t", {"k": "é"}, result="héllo wörld", error=None)
    detail = store.detail(event_id, limit=4)
    assert detail["result"] == "héll" and detail["result_size"] == 11
    assert detail["args"] == '{"k"' and detail["args_size"] == len('{"k": "\\u00e9"}')
    assert detail["error"] is None and detail["error_size"] == 0
    assert "result_bytes" in detail and "args_preview" not in detail
    assert store.slice(event_id, "result", 4, 3) == {
        "field": "result", "offset": 4, "text": "o w", "size": 11, "next": 7,
    }  # fmt: skip
    assert store.slice(event_id, "result", 100)["text"] == ""
    assert store.slice(event_id, "error")["text"] == "" and store.slice(event_id, "error")["size"] == 0
    assert store.field_text(event_id, "result") == "héllo wörld"
    assert store.detail(999) is None and store.slice(999, "result") is None and store.field_text(999, "args") is None
    with pytest.raises(ValueError):
        store.slice(event_id, "result; DROP TABLE events")
    with pytest.raises(ValueError):
        store.field_text(event_id, "session")


def test_stats_clients(store):
    store.record("tool", "a", session="s1", client="c 1")
    store.record("tool", "a", session="s1", client="c 1")
    store.record("tool", "a", client="stateless 1")
    store.record("tool", "a", client="stateless 1")
    store.record("tool", "a", client="stateless 2")
    store.record("session", "initialize", session="s2", client="c 1")
    store.record("process", "p1", session="s9")
    stats = store.stats()
    assert stats["clients"] == 4
    assert stats["sessions"] == 2


def test_stats_totals_match_per_tool(store):
    store.record("tool", "a")
    store.record("tool", "a", ok=False, error="x")
    store.record("tool", "b")
    store.start("tool", "b")
    stats = store.stats()
    assert (stats["calls"], stats["errors"], stats["running"]) == (4, 1, 1)
    durations = [r["avg_ms"] for r in stats["per_tool"]]
    assert stats["avg_ms"] is not None and min(durations) <= stats["avg_ms"] <= max(durations)
    assert set(stats["per_tool"][0]) == {"name", "calls", "errors", "avg_ms", "max_ms"}


async def test_live_feed_gets_audit_messages_only_while_subscribed(store):
    assert not store.live.active
    queue = store.live.subscribe()
    event_id = store.start("tool", "x", {"a": TOKEN})
    kind, data = queue.get_nowait()
    assert kind == "audit" and data["id"] == event_id and data["status"] == "running"
    assert TOKEN not in data["args_preview"]
    store.live.unsubscribe(queue)
    store.finish(event_id, ok=True)
    assert queue.empty() and not store.live.active


async def test_live_feed_drops_messages_for_a_full_queue(store):
    queue = store.live.subscribe()
    for _ in range(600):
        store.record("tool", "spam")
    assert queue.full()
    store.live.publish("tasks", {"change": "task"})  # no exception, no blocking


# Retention

DAY = 86400


def _record_old(store: AuditStore, monkeypatch, name: str, days_ago: float = 40) -> int:
    """Record an event as if it happened `days_ago` days back, in SQLite and the JSONL."""
    then = time.time() - days_ago * DAY
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: then)
        return store.record("tool", name, result="old")


def test_prune_deletes_old_events_only(store, monkeypatch):
    old = [_record_old(store, monkeypatch, "old") for _ in range(3)]
    new = [store.record("tool", "new") for _ in range(2)]
    pruned = store.prune(30, store.db_path.parent / "processes")
    assert (pruned.events, pruned.jsonl_lines) == (3, 3)
    assert all(store.get(i) is None for i in old)
    assert [e["id"] for e in store.query()] == new[::-1]
    assert store.stats()["calls"] == 2
    assert store.record("tool", "after") > new[-1]  # ids keep growing past deleted rows


def test_prune_rewrites_jsonl_keeping_unreadable_lines(store, monkeypatch):
    _record_old(store, monkeypatch, "old")
    keep = store.record("tool", "new")
    with store.jsonl_path.open("ab") as f:
        f.write(b"not json\n")
        f.write(b'{"no_ts": 1}\n')
        f.write(b'{"ts": "yesterday"}\n')
        f.write(b"[1, 2]\n")
        f.write(b"\xff\xfe binary\n")
    pruned = store.prune(30, store.db_path.parent / "processes")
    assert pruned.jsonl_lines == 1
    after = store.record("tool", "after")
    lines = store.jsonl_path.read_bytes().splitlines()
    assert json.loads(lines[0])["id"] == keep
    assert lines[1:6] == [b"not json", b'{"no_ts": 1}', b'{"ts": "yesterday"}', b"[1, 2]", b"\xff\xfe binary"]
    assert json.loads(lines[6])["id"] == after  # the reopened handle appends to the rewritten file
    assert sorted(p.name for p in store.db_path.parent.iterdir() if "jsonl" in p.name) == ["audit.jsonl"]


def test_prune_with_nothing_old_changes_nothing(store, monkeypatch):
    store.record("tool", "new")
    before = store.jsonl_path.stat()
    handle = store._jsonl

    def no_temp_file(*args, **kwargs):
        raise AssertionError("a prune with nothing to drop created a temp file")

    monkeypatch.setattr(tempfile, "mkstemp", no_temp_file)
    statements: list[str] = []
    store._db.set_trace_callback(statements.append)
    pruned = store.prune(30, store.db_path.parent / "processes")
    store._db.set_trace_callback(None)
    assert (pruned.events, pruned.jsonl_lines, pruned.process_logs) == (0, 0, 0)
    assert not any("VACUUM" in s for s in statements)
    after = store.jsonl_path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not [p for p in store.db_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert store._jsonl is handle and not handle.closed


def test_failed_jsonl_rewrite_keeps_the_store_usable(store, monkeypatch):
    _record_old(store, monkeypatch, "old")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        store.prune(30, store.db_path.parent / "processes")
    monkeypatch.undo()
    after = store.record("tool", "after")
    assert [json.loads(line)["id"] for line in store.jsonl_path.read_text().splitlines()][-1] == after
    assert not [p for p in store.db_path.parent.iterdir() if p.name.endswith(".tmp")]


def test_failed_jsonl_open_closes_the_temp_file(store, monkeypatch):
    _record_old(store, monkeypatch, "old")
    real_open = type(store.jsonl_path).open
    calls = {"rb": 0}

    def open_failing_second_read(self, mode="r", *args, **kwargs):
        if self == store.jsonl_path and mode == "rb":
            calls["rb"] += 1
            if calls["rb"] == 2:
                raise OSError("source vanished")
        return real_open(self, mode, *args, **kwargs)

    fds_before = set(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(type(store.jsonl_path), "open", open_failing_second_read)
    with pytest.raises(OSError, match="source vanished"):
        store.prune(30, store.db_path.parent / "processes")
    monkeypatch.undo()
    # The reopened append handle replaces the closed one, so the count matches; a leak adds one.
    assert len(set(os.listdir("/proc/self/fd"))) <= len(fds_before)
    assert not [p for p in store.db_path.parent.iterdir() if p.name.endswith(".tmp")]
    after = store.record("tool", "after")
    assert [json.loads(line)["id"] for line in store.jsonl_path.read_text().splitlines()][-1] == after


def test_prune_vacuums_after_deleting_when_asked(store, monkeypatch):
    _record_old(store, monkeypatch, "old")
    statements: list[str] = []
    store._db.set_trace_callback(statements.append)
    store.prune(30, store.db_path.parent / "processes", vacuum=True)
    store._db.set_trace_callback(None)
    assert "VACUUM" in statements
    assert (store.db_path.parent / "audit.sqlite3-wal").stat().st_size == 0


def test_prune_logs_and_survives_a_failed_vacuum(store, monkeypatch, caplog):
    old = _record_old(store, monkeypatch, "old")
    new = store.record("tool", "new")
    # A statement still stepping on the same connection makes SQLite refuse VACUUM.
    pending = store._db.execute("SELECT id FROM events")
    pending.fetchone()
    pruned = store.prune(30, store.db_path.parent / "processes", vacuum=True)
    pending.close()
    assert pruned.events == 1 and store.get(old) is None and store.get(new) is not None
    assert "VACUUM failed" in caplog.text and "statements in progress" in caplog.text


def test_prune_does_not_vacuum_by_default(store, monkeypatch):
    """VACUUM rewrites the whole file under the store lock, so passes while serving skip it."""
    _record_old(store, monkeypatch, "old")
    statements: list[str] = []
    store._db.set_trace_callback(statements.append)
    assert store.prune(30, store.db_path.parent / "processes").events == 1
    store._db.set_trace_callback(None)
    assert not any("VACUUM" in s or "wal_checkpoint" in s for s in statements)


def test_prune_deletes_in_batches(store, monkeypatch):
    """Each batch is one short hold of the store lock, so writes get in between them."""
    old = [_record_old(store, monkeypatch, "old") for _ in range(5)]
    new = store.record("tool", "new")
    monkeypatch.setattr(audit_module, "_PRUNE_BATCH", 2)
    deletes: list[str] = []
    store._db.set_trace_callback(lambda s: deletes.append(s) if s.startswith("DELETE") else None)
    assert store.prune(30, store.db_path.parent / "processes").events == 5
    store._db.set_trace_callback(None)
    assert len(deletes) == 3
    assert all(store.get(i) is None for i in old) and store.get(new) is not None


def test_prune_removes_old_process_logs(store, tmp_path):
    logs = tmp_path / "processes"
    logs.mkdir()
    old_log, fresh_log, old_other = logs / "web-1.log", logs / "web-2.log", logs / "notes.txt"
    for path in (old_log, fresh_log, old_other):
        path.write_text("x")
    old = time.time() - 40 * DAY
    for path in (old_log, old_other):
        os.utime(path, (old, old))
    assert store.prune(30, logs).process_logs == 1
    assert sorted(p.name for p in logs.iterdir()) == ["notes.txt", "web-2.log"]


def test_prune_without_process_log_dir(store, tmp_path):
    assert store.prune(30, tmp_path / "missing").process_logs == 0


def test_runtime_prunes_with_configured_days(tmp_path, workspace, monkeypatch, caplog):
    state = tmp_path / "state"
    cfg = Config(workspace=workspace, state_dir=state, sandbox={"backend": "none"}, audit={"retention_days": 7})
    cfg = cfg.finalize()
    seed = AuditStore(state, secrets=[], result_bytes=100)
    week_old = _record_old(seed, monkeypatch, "eight", days_ago=8)
    recent = _record_old(seed, monkeypatch, "six", days_ago=6)
    seed.close()
    caplog.set_level(logging.INFO, logger="dev_link_mcp.runtime")
    rt = Runtime.create(cfg)
    try:
        assert rt.audit.get(week_old) is None and rt.audit.get(recent) is not None
    finally:
        rt.audit.close()
    assert "removed audit data older than 7 days: 1 events, 1 JSONL lines, 0 process logs" in caplog.text


def test_runtime_starts_when_pruning_fails(tmp_path, workspace, monkeypatch, caplog):
    state = tmp_path / "state"
    cfg = Config(workspace=workspace, state_dir=state, sandbox={"backend": "none"}).finalize()
    seed = AuditStore(state, secrets=[], result_bytes=100)
    _record_old(seed, monkeypatch, "old")
    seed.close()

    def disk_full(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "replace", disk_full)
    caplog.set_level(logging.WARNING, logger="dev_link_mcp.runtime")
    rt = Runtime.create(cfg)
    monkeypatch.undo()
    try:
        assert "could not prune old audit data, will retry at the next start: No space left on device" in caplog.text
        after = rt.audit.record("tool", "after")
        assert rt.audit.get(after) is not None
        assert json.loads(rt.audit.jsonl_path.read_text().splitlines()[-1])["id"] == after
    finally:
        rt.audit.close()


def test_runtime_keeps_everything_without_retention(tmp_path, workspace, monkeypatch):
    state = tmp_path / "state"
    cfg = Config(workspace=workspace, state_dir=state, sandbox={"backend": "none"}, audit={"retention_days": None})
    cfg = cfg.finalize()
    seed = AuditStore(state, secrets=[], result_bytes=100)
    ancient = _record_old(seed, monkeypatch, "ancient", days_ago=4000)
    seed.close()
    rt = Runtime.create(cfg)
    try:
        assert rt.audit.get(ancient) is not None
    finally:
        rt.audit.close()
