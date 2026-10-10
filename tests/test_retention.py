"""Audit retention while the server runs: periodic pruning, live process logs, concurrent writes."""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from dev_link_mcp import audit as audit_module
from dev_link_mcp.audit import AuditStore
from dev_link_mcp.config import Config
from dev_link_mcp.processes import LOG_DIR
from dev_link_mcp.runtime import Runtime

DAY = 86400


def _record_old(store: AuditStore, monkeypatch: pytest.MonkeyPatch, name: str, days_ago: float = 40) -> int:
    """Record an event as if it happened `days_ago` days back, in SQLite and the JSONL."""
    then = time.time() - days_ago * DAY
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: then)
        return store.record("tool", name, result="old")


def _age(path: Path, days: float = 40) -> None:
    then = time.time() - days * DAY
    os.utime(path, (then, then))


@pytest.fixture
def store(tmp_path):
    s = AuditStore(tmp_path, secrets=[], result_bytes=1000)
    yield s
    s.close()


@pytest.fixture
async def rt(tmp_path, workspace):
    cfg = Config(workspace=workspace, state_dir=tmp_path / "state", sandbox={"backend": "none"}).finalize()
    runtime = Runtime.create(cfg)
    yield runtime
    await runtime.processes.shutdown()
    runtime.audit.close()


async def _until(check, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


def test_prune_keeps_listed_logs_even_when_old(store, tmp_path):
    logs = tmp_path / "processes"
    logs.mkdir()
    live, dead = logs / "p1-1.log", logs / "p2-1.log"
    for path in (live, dead):
        path.write_text("x")
        _age(path)
    assert store.prune(30, logs, keep={live}).process_logs == 1
    assert [p.name for p in logs.iterdir()] == ["p1-1.log"]


def test_jsonl_write_during_a_prune_is_not_lost(store, monkeypatch):
    """A finish() that is about to append while another thread prunes must land in the rewritten file."""
    _record_old(store, monkeypatch, "old")
    event_id = store.start("tool", "concurrent")
    reached_replace, written = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def prune() -> None:
        try:
            store.prune(30, store.db_path.parent / "missing")
        except BaseException as exc:  # pragma: no cover - reported by the assertion below
            errors.append(exc)

    real_replace, real_dumps = os.replace, json.dumps

    def slow_replace(*args, **kwargs):
        reached_replace.set()
        written.wait(2)
        return real_replace(*args, **kwargs)

    worker = threading.Thread(target=prune)

    def dumps_then_prune(obj, *args, **kwargs):
        # The finished event is serialized right before it is appended: start a prune and give it
        # every chance to rewrite the file in between.
        if isinstance(obj, dict) and obj.get("id") == event_id and not worker.is_alive():
            worker.start()
            reached_replace.wait(1)
        return real_dumps(obj, *args, **kwargs)

    monkeypatch.setattr(audit_module.os, "replace", slow_replace)
    monkeypatch.setattr(audit_module.json, "dumps", dumps_then_prune)
    try:
        store.finish(event_id, ok=True, result="done")
    finally:
        written.set()
        worker.join(5)
    monkeypatch.undo()
    assert not errors
    ids = [json.loads(line)["id"] for line in store.jsonl_path.read_text().splitlines()]
    assert ids == [event_id]


def test_writes_do_not_wait_for_the_jsonl_copy(store, monkeypatch):
    """The bulk of the JSONL rewrite runs without the store lock; events recorded meanwhile are kept."""
    _record_old(store, monkeypatch, "old")
    kept = store.record("tool", "kept")
    copying, release = threading.Event(), threading.Event()
    real_older = audit_module._older
    calls = 0

    def paused_older(line, cutoff):
        nonlocal calls
        calls += 1
        # The first pass only checks for old lines; pause in the second, which copies.
        if calls == 3:
            copying.set()
            release.wait(5)
        return real_older(line, cutoff)

    monkeypatch.setattr(audit_module, "_older", paused_older)
    errors: list[BaseException] = []

    def prune() -> None:
        try:
            store.prune(30, store.db_path.parent / "missing")
        except BaseException as exc:  # pragma: no cover - reported by the assertion below
            errors.append(exc)

    worker = threading.Thread(target=prune)
    worker.start()
    try:
        assert copying.wait(5)
        recorded: list[int] = []
        writer = threading.Thread(target=lambda: recorded.append(store.record("tool", "during")))
        writer.start()
        writer.join(1)
        assert recorded, "record() waited for the JSONL copy"
    finally:
        release.set()
        worker.join(5)
    assert not errors
    after = store.record("tool", "after")
    ids = [json.loads(line)["id"] for line in store.jsonl_path.read_text().splitlines()]
    assert ids == [kept, recorded[0], after]


async def test_periodic_prune_removes_old_data_while_running(rt, monkeypatch):
    old = _record_old(rt.audit, monkeypatch, "old")
    stale_log = rt.config.state_dir / LOG_DIR / "p9-1.log"
    stale_log.write_text("old output")
    _age(stale_log)
    async with rt.prune_periodically(interval_s=0.05):
        await _until(lambda: rt.audit.get(old) is None and not stale_log.exists())
    assert all(json.loads(line)["id"] != old for line in rt.audit.jsonl_path.read_text().splitlines())


async def test_periodic_prune_keeps_the_logs_of_known_processes(rt, monkeypatch):
    running = await rt.processes.start("sleep 30", cwd=rt.workspace.root)
    exited = await rt.processes.start("true", cwd=rt.workspace.root)
    await _until(lambda: not exited.running)
    for managed in (running, exited):
        _age(managed.log_path)
    stale_log = rt.config.state_dir / LOG_DIR / "p9-1.log"
    stale_log.write_text("old output")
    _age(stale_log)
    async with rt.prune_periodically(interval_s=0.05):
        await _until(lambda: not stale_log.exists())
    # A running process's output and an exited one that list_processes still shows stay readable.
    assert running.log_path.exists() and exited.log_path.exists()


async def test_periodic_prune_survives_a_failure(rt, monkeypatch, caplog):
    old = _record_old(rt.audit, monkeypatch, "old")
    calls = 0
    real_prune = rt.audit.prune

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("No space left on device")
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(rt.audit, "prune", fail_once)
    caplog.set_level(logging.WARNING, logger="dev_link_mcp.runtime")
    async with rt.prune_periodically(interval_s=0.05):
        await _until(lambda: rt.audit.get(old) is None)
    assert calls >= 2
    assert "could not prune old audit data, will retry in" in caplog.text
    assert "No space left on device" in caplog.text


async def test_periodic_prune_ignores_a_deleted_process_log(rt, monkeypatch, caplog):
    """The logs to keep come from the process list without reading the disk, so a deleted one is no error."""
    old = _record_old(rt.audit, monkeypatch, "old")
    managed = await rt.processes.start("true", cwd=rt.workspace.root)
    await _until(lambda: not managed.running)
    managed.log_path.unlink()
    caplog.set_level(logging.WARNING, logger="dev_link_mcp.runtime")
    async with rt.prune_periodically(interval_s=0.05):
        await _until(lambda: rt.audit.get(old) is None)
    assert "could not prune" not in caplog.text


async def test_periodic_prune_survives_a_failure_collecting_logs_to_keep(rt, monkeypatch, caplog):
    old = _record_old(rt.audit, monkeypatch, "old")
    real_log_paths = rt.processes.log_paths
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("process table changed")
        return real_log_paths()

    monkeypatch.setattr(rt.processes, "log_paths", fail_once)
    caplog.set_level(logging.WARNING, logger="dev_link_mcp.runtime")
    async with rt.prune_periodically(interval_s=0.05):
        await _until(lambda: rt.audit.get(old) is None)
    assert calls >= 2 and "process table changed" in caplog.text


async def test_periodic_prune_task_is_gone_after_exit(rt):
    def pruners() -> list[asyncio.Task]:
        return [t for t in asyncio.all_tasks() if t.get_name() == "audit retention"]

    async with rt.prune_periodically(interval_s=0.05):
        assert len(pruners()) == 1
        await asyncio.sleep(0.12)
    assert pruners() == []


async def test_exit_waits_for_a_prune_in_progress(rt, monkeypatch):
    """Leaving must not return while a prune thread still holds the store, or close() would race it."""
    _record_old(rt.audit, monkeypatch, "old")
    started, release = threading.Event(), threading.Event()
    finished: list[bool] = []
    real_prune = rt.audit.prune

    def slow_prune(*args, **kwargs):
        started.set()
        release.wait(5)
        result = real_prune(*args, **kwargs)
        finished.append(True)
        return result

    monkeypatch.setattr(rt.audit, "prune", slow_prune)
    async with rt.prune_periodically(interval_s=0.01):
        await _until(started.is_set)
        threading.Timer(0.2, release.set).start()
    assert finished == [True]


async def test_no_periodic_prune_without_retention(tmp_path, workspace, monkeypatch):
    cfg = Config(
        workspace=workspace, state_dir=tmp_path / "state", sandbox={"backend": "none"}, audit={"retention_days": None}
    ).finalize()
    runtime = Runtime.create(cfg)
    try:
        ancient = _record_old(runtime.audit, monkeypatch, "ancient", days_ago=4000)
        async with runtime.prune_periodically(interval_s=0.01):
            assert not [t for t in asyncio.all_tasks() if t.get_name() == "audit retention"]
            await asyncio.sleep(0.05)
        assert runtime.audit.get(ancient) is not None
    finally:
        runtime.audit.close()
