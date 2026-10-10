"""Audit store: every tool call, session and process event, persisted and streamed live.

Events go to SQLite (queried by the dashboard) and to an append-only JSONL file
(for grep and external log shippers). Both live in the state directory, outside
the workspace. Subscribers get each change as it happens for the live view.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_link_mcp.live import LiveFeed

log = logging.getLogger(__name__)

_SECRET_KEY = re.compile(r"(secret|token|passw(or)?d|api[_-]?key|authorization|cookie|credential)", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ended REAL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    session TEXT,
    client TEXT,
    args TEXT,
    status TEXT NOT NULL,
    duration_ms REAL,
    result TEXT,
    result_bytes INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_name ON events(name);
CREATE INDEX IF NOT EXISTS events_status ON events(status);
-- Covers every stats query, so they read this index instead of rows that carry large results.
CREATE INDEX IF NOT EXISTS events_stats ON events(kind, ts, name, status, duration_ms, session, client);
"""

_COLUMNS = (
    "id",
    "ts",
    "ended",
    "kind",
    "name",
    "session",
    "client",
    "args",
    "status",
    "duration_ms",
    "result",
    "result_bytes",
    "error",
)
_SUMMARY_COLUMNS = (
    "id",
    "ts",
    "ended",
    "kind",
    "name",
    "session",
    "client",
    "status",
    "duration_ms",
    "result_bytes",
    "error",
)
# Stored text fields the dashboard reads in slices; also the only names allowed in their SQL.
SLICE_FIELDS = ("args", "result", "error")
SLICE_DEFAULT = 65536
SLICE_MAX = 1048576


@dataclass(frozen=True)
class Pruned:
    """What one AuditStore.prune() call removed."""

    events: int
    jsonl_lines: int
    process_logs: int


class AuditStore:
    """Thread-safe event log backed by SQLite plus a JSONL mirror.

    Event kinds: "tool" (an MCP tools/call), "session" (client connect), "process"
    (background process lifecycle) and "server" (start/stop). Status is "running",
    "ok" or "error".
    """

    def __init__(self, state_dir: Path, *, secrets: list[str], result_bytes: int):
        self.db_path = state_dir / "audit.sqlite3"
        self.jsonl_path = state_dir / "audit.jsonl"
        self._secrets = [s for s in secrets if s]
        self._result_bytes = result_bytes
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")
        # Dashboard streams get every change here as an "audit" message, next to process and task messages.
        self.live = LiveFeed()

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: "***" if _SECRET_KEY.search(str(k)) and isinstance(v, str) else self._redact_value(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._redact_value(v) for v in value]
        if isinstance(value, str):
            return self.redact(value)
        return value

    def _clip(self, text: str | None) -> tuple[str | None, int | None]:
        if text is None:
            return None, None
        text = self.redact(text)
        size = len(text.encode("utf-8", errors="replace"))
        if size > self._result_bytes:
            text = text[: self._result_bytes] + f"\n\n... [clipped in audit log, {size} bytes total]"
        return text, size

    def start(
        self, kind: str, name: str, args: Any = None, *, session: str | None = None, client: str | None = None
    ) -> int:
        """Insert a running event and notify subscribers. Returns the event id.

        `args` is stored as redacted JSON, clipped like results to `result_bytes` (then it is no
        longer valid JSON and ends with a clipped note).
        """
        args_json = None
        if args is not None:
            # Arguments can be as large as results (file contents, process input), so they share the budget.
            args_json, _ = self._clip(json.dumps(self._redact_value(args), default=str))
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events(ts, kind, name, session, client, args, status) VALUES (?,?,?,?,?,?,?)",
                (time.time(), kind, name, session, client, args_json, "running"),
            )
            event_id = int(cur.lastrowid or 0)
        self._publish(event_id)
        return event_id

    def finish(self, event_id: int, *, ok: bool, result: str | None = None, error: str | None = None) -> None:
        """Close a running event with its outcome and notify subscribers."""
        result_text, size = self._clip(result)
        error_text = self.redact(error)[:4000] if error else None
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT ts FROM events WHERE id=?", (event_id,)).fetchone()
            duration = (now - row["ts"]) * 1000 if row else None
            self._db.execute(
                "UPDATE events SET ended=?, status=?, duration_ms=?, result=?, result_bytes=?, error=? WHERE id=?",
                (now, "ok" if ok else "error", duration, result_text, size, error_text, event_id),
            )
        event = self.get(event_id)
        if event:
            self._jsonl.write(json.dumps(event, default=str) + "\n")
            self._jsonl.flush()
        self._publish(event_id)

    def record(
        self,
        kind: str,
        name: str,
        args: Any = None,
        *,
        ok: bool = True,
        result: str | None = None,
        error: str | None = None,
        session: str | None = None,
        client: str | None = None,
    ) -> int:
        """One-shot event: start and finish in one step."""
        event_id = self.start(kind, name, args, session=session, client=client)
        self.finish(event_id, ok=ok, result=result, error=error)
        return event_id

    def get(self, event_id: int) -> dict[str, Any] | None:
        with self._lock:
            # Column lists are module constants; only values are user input and those are bound.
            sql = f"SELECT {','.join(_COLUMNS)} FROM events WHERE id=?"  # noqa: S608
            row = self._db.execute(sql, (event_id,)).fetchone()
        return self._row(row) if row else None

    def query(
        self,
        *,
        limit: int = 100,
        before: int | None = None,
        after: int | None = None,
        kind: str | None = None,
        name: str | None = None,
        status: str | None = None,
        session: str | None = None,
        client: str | None = None,
        text: str | None = None,
    ) -> list[dict[str, Any]]:
        """Event summaries (no args/result bodies) matching all given filters, at most 1000.

        Keyset pages: `before` gives events older than that id, newest first. `after` gives
        events newer than that id, oldest first, for catching up after a reconnect. Without
        `after` the order is newest first.
        """
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("kind", kind),
            ("name", name),
            ("status", status),
            ("session", session),
            ("client", client),
        ):
            if value:
                where.append(f"{column}=?")
                params.append(value)
        if before:
            where.append("id<?")
            params.append(before)
        if after is not None:
            where.append("id>?")
            params.append(after)
        if text:
            where.append("(args LIKE ? OR result LIKE ? OR error LIKE ? OR name LIKE ?)")
            params += [f"%{text}%"] * 4
        sql = f"SELECT {','.join(_SUMMARY_COLUMNS)}, substr(args,1,300) AS args_preview FROM events"  # noqa: S608
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC LIMIT ?" if after is not None else " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def stats(self, since_s: float = 86400) -> dict[str, Any]:
        """Aggregates for the dashboard over the last `since_s` seconds.

        Counts cover tool calls. `sessions` counts distinct session ids of tool calls and client
        handshakes; `clients` counts distinct callers among them, by session id where there is
        one and by client label otherwise.
        """
        since = time.time() - since_s
        with self._lock:
            per_tool = self._db.execute(
                "SELECT name, count(*) AS calls, sum(status='error') AS errors, sum(status='running') AS running,"
                " avg(duration_ms) AS avg_ms, max(duration_ms) AS max_ms, sum(duration_ms) AS sum_ms,"
                " count(duration_ms) AS timed FROM events WHERE kind='tool' AND ts>=? GROUP BY name"
                " ORDER BY calls DESC",
                (since,),
            ).fetchall()
            timeline = self._db.execute(
                "SELECT CAST(ts/300 AS INTEGER)*300 AS bucket, count(*) AS calls, sum(status='error') AS errors"
                " FROM events WHERE kind='tool' AND ts>=? GROUP BY bucket ORDER BY bucket",
                (since,),
            ).fetchall()
            # Stateless clients send no session id, so they are told apart by their client label.
            people = self._db.execute(
                "SELECT count(DISTINCT session) AS sessions, count(DISTINCT coalesce(session, client)) AS clients"
                " FROM events WHERE kind IN ('tool','session') AND ts>=?",
                (since,),
            ).fetchone()
        # The totals are sums over the per-tool rows, which saves a third pass over the range.
        timed = sum(r["timed"] for r in per_tool)
        return {
            "since": since,
            "calls": sum(r["calls"] for r in per_tool),
            "errors": sum(r["errors"] for r in per_tool),
            "running": sum(r["running"] for r in per_tool),
            "avg_ms": sum(r["sum_ms"] or 0 for r in per_tool) / timed if timed else None,
            "sessions": people["sessions"],
            "clients": people["clients"],
            "per_tool": [{k: r[k] for k in ("name", "calls", "errors", "avg_ms", "max_ms")} for r in per_tool],
            "timeline": [dict(r) for r in timeline],
        }

    def detail(self, event_id: int, limit: int = SLICE_DEFAULT) -> dict[str, Any] | None:
        """Event metadata plus the first `limit` characters of args, result and error.

        Each field comes as `<field>` (the text slice, or None when unset) and `<field>_size`
        (its full length in characters, 0 when unset), so a large result is never sent whole.
        Returns None for an unknown id.
        """
        limit = max(1, min(limit, SLICE_MAX))
        meta = [c for c in _SUMMARY_COLUMNS if c not in SLICE_FIELDS]
        parts = [f"substr({f},1,?) AS {f}, coalesce(length({f}),0) AS {f}_size" for f in SLICE_FIELDS]
        sql = f"SELECT {','.join(meta)}, {','.join(parts)} FROM events WHERE id=?"  # noqa: S608
        with self._lock:
            row = self._db.execute(sql, (*([limit] * len(SLICE_FIELDS)), event_id)).fetchone()
        return dict(row) if row else None

    def slice(self, event_id: int, field: str, offset: int = 0, limit: int = SLICE_DEFAULT) -> dict[str, Any] | None:
        """Characters [offset, offset+limit) of one stored field of an event.

        Offsets count characters (Unicode code points), not bytes, so a slice never splits a
        character. `limit` is capped at SLICE_MAX. Returns {"field", "offset", "text", "size",
        "next"} where `next` is the offset after this slice and `size` the full length, or None
        for an unknown id. Raises ValueError if `field` is not in SLICE_FIELDS.
        """
        if field not in SLICE_FIELDS:
            raise ValueError(f"field must be one of {', '.join(SLICE_FIELDS)}")
        offset = max(0, offset)
        limit = max(1, min(limit, SLICE_MAX))
        sql = f"SELECT substr({field},?,?) AS text, coalesce(length({field}),0) AS size FROM events WHERE id=?"  # noqa: S608
        with self._lock:
            row = self._db.execute(sql, (offset + 1, limit, event_id)).fetchone()
        if row is None:
            return None
        text = row["text"] or ""
        return {"field": field, "offset": offset, "text": text, "size": row["size"], "next": offset + len(text)}

    def field_text(self, event_id: int, field: str) -> str | None:
        """The whole stored value of one field ("" when unset), or None for an unknown id.

        Raises ValueError if `field` is not in SLICE_FIELDS.
        """
        if field not in SLICE_FIELDS:
            raise ValueError(f"field must be one of {', '.join(SLICE_FIELDS)}")
        with self._lock:
            row = self._db.execute(f"SELECT {field} FROM events WHERE id=?", (event_id,)).fetchone()  # noqa: S608
        if row is None:
            return None
        return row[0] or ""

    def mark_interrupted(self) -> None:
        """Close events left running by a previous crash so the dashboard does not show them as live."""
        with self._lock:
            self._db.execute(
                "UPDATE events SET status='error', error='server stopped before the call finished'"
                " WHERE status='running'"
            )

    def prune(self, days: int, process_logs: Path) -> Pruned:
        """Delete everything older than `days` days: SQLite events, process logs and JSONL lines, in that order.

        `process_logs` is the directory of background process logs; it may not exist. Process
        logs go by mtime, so the log of a process that is still writing is kept. They go before
        the JSONL because rewriting it needs free space for a copy. Rewrites audit.jsonl, so
        only one store may have the state dir open; call this at startup, before anything is
        recorded. Raises sqlite3.Error if the DELETE fails (nothing else is pruned then) and
        OSError if the JSONL cannot be rewritten; the store stays usable either way.
        """
        cutoff = time.time() - days * 86400
        with self._lock:
            events = self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            if events:
                try:
                    self._db.execute("VACUUM")
                except sqlite3.OperationalError as exc:
                    # The rows are gone either way; the file just keeps its free pages until a later VACUUM.
                    log.warning("audit store VACUUM failed, the file keeps its size: %s", exc)
                else:
                    # VACUUM writes the whole database through the WAL, which otherwise stays that
                    # size on disk until the server stops.
                    self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logs = _prune_logs(process_logs, cutoff)
            lines = self._prune_jsonl(cutoff)
        return Pruned(events=events, jsonl_lines=lines, process_logs=logs)

    def _prune_jsonl(self, cutoff: float) -> int:
        """Rewrite audit.jsonl without lines whose `ts` is before `cutoff`. Caller holds the lock.

        Leaves the file and the append handle alone when no line is old enough.
        """
        # A read-only pass first, so the usual startup with nothing to drop writes nothing.
        # Bytes, so a line that is not valid UTF-8 is kept as it is instead of failing the prune.
        with self.jsonl_path.open("rb") as src:
            if not any(_older(line, cutoff) for line in src):
                return 0
        fd, tmp_name = tempfile.mkstemp(dir=self.jsonl_path.parent, prefix=".audit.jsonl.", suffix=".tmp")
        tmp = Path(tmp_name)
        dropped = 0
        try:
            # The temp fd is wrapped first so it is closed even when opening the source fails.
            with os.fdopen(fd, "wb") as dst:
                self._jsonl.close()
                with self.jsonl_path.open("rb") as src:
                    for line in src:
                        if _older(line, cutoff):
                            dropped += 1
                        else:
                            dst.write(line)
                if dropped:
                    # On disk before the rename, so a crash cannot swap in an empty file.
                    dst.flush()
                    os.fsync(dst.fileno())
            if dropped:
                os.replace(tmp, self.jsonl_path)
        finally:
            tmp.unlink(missing_ok=True)
            self._jsonl = self.jsonl_path.open("a", encoding="utf-8")
        return dropped

    def _publish(self, event_id: int) -> None:
        if not self.live.active:
            return
        with self._lock:
            row = self._db.execute(
                f"SELECT {','.join(_SUMMARY_COLUMNS)}, substr(args,1,300) AS args_preview FROM events WHERE id=?",  # noqa: S608
                (event_id,),
            ).fetchone()
        if row is None:
            return
        self.live.publish("audit", dict(row))

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        if event.get("args"):
            with contextlib.suppress(json.JSONDecodeError):
                event["args"] = json.loads(event["args"])
        return event

    def close(self) -> None:
        with self._lock:
            self._db.close()
        self._jsonl.close()


def _older(line: bytes, cutoff: float) -> bool:
    """Whether a JSONL line is an event from before `cutoff`. Lines that cannot be read as one are kept."""
    try:
        event = json.loads(line)
    except ValueError:
        return False
    ts = event.get("ts") if isinstance(event, dict) else None
    return isinstance(ts, int | float) and not isinstance(ts, bool) and ts < cutoff


def _prune_logs(directory: Path, cutoff: float) -> int:
    """Remove `*.log` files in `directory` last modified before `cutoff`; returns how many."""
    removed = 0
    for path in directory.glob("*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            # Gone already, or not ours to remove; the rest still get cleaned.
            continue
    return removed


class AuditReader:
    """Read-only view of the audit store in a state dir, for `dev-link logs audit`.

    Opens the SQLite file with mode=ro, so it never changes the store and works next to a running
    server; SQLite may still create the store's empty -wal and -shm side files, as any reader of
    a WAL database does. Raises FileNotFoundError when the store does not exist yet.
    Rows are summaries: id, ts, kind, name, client, status, duration_ms.
    """

    _SQL = "SELECT id, ts, kind, name, client, status, duration_ms FROM events"

    def __init__(self, state_dir: Path):
        path = state_dir / "audit.sqlite3"
        if not path.is_file():
            raise FileNotFoundError(f"no audit store at {path}")
        self._db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row

    def latest(self, limit: int) -> list[dict[str, Any]]:
        """The newest `limit` events, oldest first."""
        rows = self._db.execute(f"{self._SQL} ORDER BY id DESC LIMIT ?", (max(limit, 0),)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_id(self) -> int:
        """Id of the newest event, or 0 for an empty store."""
        row = self._db.execute("SELECT MAX(id) FROM events").fetchone()
        return int(row[0] or 0)

    def after(self, event_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        """Events newer than `event_id`, oldest first: the same keyset as AuditStore.query(after=...)."""
        rows = self._db.execute(f"{self._SQL} WHERE id>? ORDER BY id ASC LIMIT ?", (event_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def by_ids(self, ids: list[int]) -> list[dict[str, Any]]:
        """The events with these ids, oldest first; ids that do not exist are skipped."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._db.execute(f"{self._SQL} WHERE id IN ({marks}) ORDER BY id", ids).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._db.close()
