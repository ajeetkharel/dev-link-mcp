import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from conftest import text

from dev_link_mcp.tools.data import _run_db, _value_type


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT DEFAULT 'x@y', avatar BLOB);
        CREATE INDEX idx_users_name ON users(name);
        CREATE VIEW user_names AS SELECT name FROM users;
        CREATE TRIGGER trg AFTER INSERT ON users BEGIN SELECT 1; END;
        INSERT INTO users (name, email, avatar) VALUES ('alice', 'a@x', x'0102'), ('bob', NULL, NULL);
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db(workspace: Path) -> Path:
    path = workspace / "app.db"
    make_db(path)
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    target = tmp_path / "outside"
    target.mkdir()
    make_db(target / "secret.db")
    (target / "secret.json").write_text('{"secret": 1}')
    (target / "secret.csv").write_text("a,b\n1,2\n")
    return target


# --- sqlite_query -----------------------------------------------------------------


async def test_sqlite_query_select_table(connect, db):
    async with connect("data") as client:
        r = await client.call_tool(
            "sqlite_query", {"db_path": "app.db", "sql": "SELECT id, name, email, avatar FROM users"}
        )
    assert not r.is_error, text(r)
    out = text(r)
    lines = out.splitlines()
    assert lines[0].split(" | ")[0].strip() == "id"
    assert "alice" in out and "bob" in out
    assert "NULL" in out
    assert "<blob 2 bytes>" in out
    assert out.endswith("(2 rows)")


async def test_sqlite_query_params_positional_and_named(connect, db):
    async with connect("data") as client:
        r1 = await client.call_tool(
            "sqlite_query", {"db_path": "app.db", "sql": "SELECT name FROM users WHERE id = ?", "params": [2]}
        )
        r2 = await client.call_tool(
            "sqlite_query",
            {"db_path": "app.db", "sql": "SELECT name FROM users WHERE name = :n", "params": {"n": "alice"}},
        )
    assert "bob" in text(r1) and "alice" not in text(r1)
    assert "alice" in text(r2) and "(1 rows)" in text(r2)


async def test_sqlite_query_max_rows_truncates_and_counts(connect, workspace):
    conn = sqlite3.connect(workspace / "big.db")
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    conn.commit()
    conn.close()
    async with connect("data") as client:
        r = await client.call_tool("sqlite_query", {"db_path": "big.db", "sql": "SELECT n FROM t", "max_rows": 10})
    out = text(r)
    assert "500 rows, showing first 10" in out
    assert len([line for line in out.splitlines() if line.strip().isdigit()]) == 10


async def test_sqlite_query_max_rows_bounds(connect, db):
    async with connect("data") as client:
        r0 = await client.call_tool("sqlite_query", {"db_path": "app.db", "sql": "SELECT 1", "max_rows": 0})
        r_big = await client.call_tool("sqlite_query", {"db_path": "app.db", "sql": "SELECT 1", "max_rows": 10_001})
    assert r0.is_error and r_big.is_error


async def test_sqlite_query_is_read_only_by_default(connect, db):
    async with connect("data") as client:
        r = await client.call_tool("sqlite_query", {"db_path": "app.db", "sql": "DELETE FROM users"})
    assert r.is_error
    assert "write=true" in text(r)
    assert sqlite3.connect(db).execute("SELECT count(*) FROM users").fetchone()[0] == 2


async def test_sqlite_query_write_commits(connect, db):
    async with connect("data") as client:
        r = await client.call_tool(
            "sqlite_query",
            {"db_path": "app.db", "sql": "INSERT INTO users (name) VALUES (?)", "params": ["carol"], "write": True},
        )
        r2 = await client.call_tool(
            "sqlite_query",
            {"db_path": "app.db", "sql": "UPDATE users SET name = 'x' WHERE id = 1 RETURNING id, name", "write": True},
        )
    assert not r.is_error, text(r)
    assert "1 rows affected (committed)" in text(r)
    assert "(1 rows)" in text(r2)
    names = {row[0] for row in sqlite3.connect(db).execute("SELECT name FROM users")}
    assert names == {"x", "bob", "carol"}


async def test_sqlite_query_write_creates_new_db(connect, workspace):
    async with connect("data") as client:
        r = await client.call_tool("sqlite_query", {"db_path": "new.db", "sql": "CREATE TABLE t (a)", "write": True})
    assert not r.is_error, text(r)
    assert (workspace / "new.db").is_file()


async def test_sqlite_query_missing_db_without_write(connect, workspace):
    async with connect("data") as client:
        r = await client.call_tool("sqlite_query", {"db_path": "nope.db", "sql": "SELECT 1"})
        r2 = await client.call_tool(
            "sqlite_query", {"db_path": "missing/dir/x.db", "sql": "CREATE TABLE t (a)", "write": True}
        )
    assert r.is_error and "does not exist" in text(r)
    assert r2.is_error and "parent directory does not exist" in text(r2)
    assert not (workspace / "nope.db").exists()


async def test_sqlite_query_errors(connect, workspace, db):
    (workspace / "notdb.db").write_text("hello, this is not sqlite" * 10)
    (workspace / "dir.db").mkdir()
    async with connect("data") as client:
        syntax = await client.call_tool("sqlite_query", {"db_path": "app.db", "sql": "SELEC 1"})
        multi = await client.call_tool("sqlite_query", {"db_path": "app.db", "sql": "SELECT 1; SELECT 2"})
        notdb = await client.call_tool("sqlite_query", {"db_path": "notdb.db", "sql": "SELECT name FROM sqlite_schema"})
        isdir = await client.call_tool("sqlite_query", {"db_path": "dir.db", "sql": "SELECT 1"})
    assert syntax.is_error and "SQLite error" in text(syntax)
    assert multi.is_error and "single statement" in text(multi)
    assert notdb.is_error and "not a database" in text(notdb)
    assert isdir.is_error and "not a file" in text(isdir)


@pytest.mark.parametrize("sql", ["ATTACH DATABASE '{p}' AS e", "VACUUM INTO '{p}'"], ids=["attach", "vacuum-into"])
async def test_sqlite_query_blocks_files_outside_workspace(connect, db, tmp_path, sql):
    target = tmp_path / "escape.db"
    async with connect("data") as client:
        r = await client.call_tool("sqlite_query", {"db_path": "app.db", "sql": sql.format(p=target), "write": True})
    assert r.is_error
    assert "not allowed" in text(r)
    assert not target.exists()


async def test_sqlite_query_timeout_stops_runaway_query(connect, db):
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    started = time.monotonic()
    async with connect("data") as client:
        r = await client.call_tool("sqlite_query", {"db_path": "app.db", "sql": sql, "timeout_s": 1})
    assert r.is_error
    assert "time limit" in text(r)
    assert time.monotonic() - started < 15


async def test_run_db_cancellation_interrupts_worker():
    seen: list[bool] = []

    def work(stopped):
        deadline = time.monotonic() + 10
        while not stopped() and time.monotonic() < deadline:
            time.sleep(0.01)
        seen.append(stopped())
        return "done"

    task = asyncio.create_task(_run_db(60, work))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(200):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == [True]


# --- sqlite_schema ----------------------------------------------------------------


async def test_sqlite_schema_lists_tables_views_indexes(connect, db):
    async with connect("data") as client:
        r = await client.call_tool("sqlite_schema", {"db_path": "app.db"})
    assert not r.is_error, text(r)
    out = text(r)
    assert out.index("table users") < out.index("view user_names")
    assert "INTEGER" in out and "'x@y'" in out
    assert "CREATE INDEX idx_users_name" in out
    assert "CREATE TRIGGER trg" in out


async def test_sqlite_schema_empty_and_missing(connect, workspace):
    sqlite3.connect(workspace / "empty.db").close()
    async with connect("data") as client:
        empty = await client.call_tool("sqlite_schema", {"db_path": "empty.db"})
        missing = await client.call_tool("sqlite_schema", {"db_path": "missing.db"})
    assert "no tables or views" in text(empty)
    assert missing.is_error


# --- json_query -------------------------------------------------------------------


async def test_json_query_filter(connect, workspace):
    (workspace / "data.json").write_text(json.dumps({"items": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}))
    async with connect("data") as client:
        r = await client.call_tool("json_query", {"path": "data.json", "expression": "[.items[].id]"})
        raw = await client.call_tool(
            "json_query", {"path": "data.json", "expression": ".items[].name", "raw_output": True}
        )
        default = await client.call_tool("json_query", {"path": "data.json"})
    assert not r.is_error, text(r)
    assert json.loads(text(r)) == [1, 2]
    assert text(raw).splitlines() == ["a", "b"]
    assert json.loads(text(default))["items"][1]["name"] == "b"


async def test_json_query_json_lines(connect, workspace):
    (workspace / "events.jsonl").write_text('{"n": 1}\n{"n": 2}\n')
    async with connect("data") as client:
        r = await client.call_tool("json_query", {"path": "events.jsonl", "expression": ".n"})
    assert text(r).splitlines() == ["1", "2"]


async def test_json_query_errors(connect, workspace):
    (workspace / "bad.json").write_text("{not json")
    (workspace / "ok.json").write_text("{}")
    (workspace / "sub").mkdir()
    async with connect("data") as client:
        bad_json = await client.call_tool("json_query", {"path": "bad.json", "expression": "."})
        bad_expr = await client.call_tool("json_query", {"path": "ok.json", "expression": ".[["})
        missing = await client.call_tool("json_query", {"path": "missing.json"})
        isdir = await client.call_tool("json_query", {"path": "sub"})
    assert bad_json.is_error and "jq failed" in text(bad_json)
    assert bad_expr.is_error and "compile error" in text(bad_expr)
    assert missing.is_error and "does not exist" in text(missing)
    assert isdir.is_error and "not a file" in text(isdir)


async def test_json_query_expression_starting_with_dash_is_not_an_option(connect, workspace):
    (workspace / "ok.json").write_text('{"a": 1}')
    async with connect("data") as client:
        r = await client.call_tool("json_query", {"path": "ok.json", "expression": "-.a"})
        r2 = await client.call_tool("json_query", {"path": "ok.json", "expression": "--rawfile"})
    assert not r.is_error, text(r)
    assert text(r) == "-1"
    assert r2.is_error and "compile error" in text(r2)


async def test_json_query_cannot_read_outside_through_jq(connect, workspace, outside):
    (workspace / "ok.json").write_text("{}")
    expr = f'input_filename, (try (import "{outside}/secret" as $s; $s) catch "blocked")'
    async with connect("data") as client:
        r = await client.call_tool("json_query", {"path": "ok.json", "expression": expr})
    # Either jq rejects the import or the sandbox hides the file; the secret value never appears.
    assert '"secret": 1' not in text(r)


# --- csv_preview ------------------------------------------------------------------


async def test_csv_preview_types_nulls_and_rows(connect, workspace):
    (workspace / "people.csv").write_text(
        "id,name,score,active,born,seen\n"
        "1,alice,1.5,true,2000-01-02,2024-01-01T10:00:00Z\n"
        "2,bob,,false,2001-03-04,2024-01-02 11:00\n"
        "3,NA,7,yes,null,2024-01-03\n"
    )
    async with connect("data") as client:
        r = await client.call_tool("csv_preview", {"path": "people.csv"})
    assert not r.is_error, text(r)
    out = text(r)
    assert "delimiter: ','" in out and "data rows: 3 rows" in out and "columns: 6" in out
    summary = {
        parts[0]: (parts[1], parts[2])
        for line in out.split("first ")[0].splitlines()
        if (parts := [p.strip() for p in line.split("|")]) and len(parts) == 3
    }
    assert summary["id"] == ("integer", "0")
    assert summary["name"] == ("string", "1")
    assert summary["score"] == ("float", "1")
    assert summary["active"] == ("boolean", "0")
    assert summary["born"] == ("date", "1")
    assert summary["seen"] == ("datetime", "0")
    assert "first 3 rows:" in out and "alice" in out


async def test_csv_preview_rows_limit_and_zero(connect, workspace):
    (workspace / "n.csv").write_text("n\n" + "".join(f"{i}\n" for i in range(100)))
    async with connect("data") as client:
        r5 = await client.call_tool("csv_preview", {"path": "n.csv", "rows": 5})
        r0 = await client.call_tool("csv_preview", {"path": "n.csv", "rows": 0})
        bad = await client.call_tool("csv_preview", {"path": "n.csv", "rows": 1001})
    assert "first 5 rows:" in text(r5) and "data rows: 100 rows" in text(r5)
    assert "rows:\n" not in text(r0) and "first" not in text(r0)
    assert bad.is_error


async def test_csv_preview_delimiters(connect, workspace):
    (workspace / "semi.csv").write_text("a;b\n1;x\n2;y\n")
    (workspace / "t.tsv").write_text("a\tb\n1\tx\n")
    (workspace / "pipe.txt").write_text("a|b\n1|x\n")
    async with connect("data") as client:
        sniffed = await client.call_tool("csv_preview", {"path": "semi.csv"})
        tsv = await client.call_tool("csv_preview", {"path": "t.tsv"})
        explicit = await client.call_tool("csv_preview", {"path": "pipe.txt", "delimiter": "|"})
        tab = await client.call_tool("csv_preview", {"path": "t.tsv", "delimiter": "\\t"})
        bad = await client.call_tool("csv_preview", {"path": "t.tsv", "delimiter": "ab"})
    assert "delimiter: ';'" in text(sniffed) and "columns: 2" in text(sniffed)
    assert "delimiter: '\\\\t'" in text(tsv) and "columns: 2" in text(tsv)
    assert "columns: 2" in text(explicit)
    assert "columns: 2" in text(tab)
    assert bad.is_error and "single character" in text(bad)


async def test_csv_preview_no_header_and_ragged_rows(connect, workspace):
    (workspace / "raw.csv").write_text("1,2,3\n4,5\n6,7,8,9\n")
    async with connect("data") as client:
        r = await client.call_tool("csv_preview", {"path": "raw.csv", "has_header": False})
    out = text(r)
    assert "column_1" in out and "column_3" in out
    assert "data rows: 3 rows" in out
    assert "2 rows have a field count different" in out


async def test_csv_preview_quoted_fields_and_bom(connect, workspace):
    (workspace / "q.csv").write_bytes('﻿name,note\n"Smith, J","line1\nline2"\n'.encode())
    async with connect("data") as client:
        r = await client.call_tool("csv_preview", {"path": "q.csv"})
    out = text(r)
    assert out.splitlines()[-3].startswith("name")
    assert "Smith, J" in out and "line1\\nline2" in out


async def test_csv_preview_errors(connect, workspace):
    (workspace / "empty.csv").write_text("")
    (workspace / "bin.csv").write_bytes(b"a,b\n\x00\x01\x02")
    (workspace / "sub").mkdir()
    async with connect("data") as client:
        empty = await client.call_tool("csv_preview", {"path": "empty.csv"})
        binary = await client.call_tool("csv_preview", {"path": "bin.csv"})
        isdir = await client.call_tool("csv_preview", {"path": "sub"})
        missing = await client.call_tool("csv_preview", {"path": "missing.csv"})
    assert empty.is_error and "empty" in text(empty)
    assert binary.is_error and "binary" in text(binary)
    assert isdir.is_error and "not a file" in text(isdir)
    assert missing.is_error and "does not exist" in text(missing)


def test_value_type_inference():
    assert _value_type("-12") == "integer"
    assert _value_type("1e5") == "float"
    assert _value_type(".5") == "float"
    assert _value_type("No") == "boolean"
    assert _value_type("2024-02-03") == "date"
    assert _value_type("2024-02-03T04:05:06.7+05:30") == "datetime"
    assert _value_type("hello") == "string"


# --- path confinement -------------------------------------------------------------


def _escape_paths(workspace: Path, outside: Path, name: str) -> list[str]:
    link = workspace / f"link-{name}"
    if not link.exists():
        link.symlink_to(outside / name)
    dir_link = workspace / "outdir"
    if not dir_link.exists():
        dir_link.symlink_to(outside)
    return [f"../outside/{name}", str(outside / name), link.name, f"outdir/{name}"]


@pytest.mark.parametrize(
    ("tool", "arg", "name", "extra"),
    [
        ("sqlite_query", "db_path", "secret.db", {"sql": "SELECT * FROM users"}),
        ("sqlite_query", "db_path", "secret.db", {"sql": "DELETE FROM users", "write": True}),
        ("sqlite_schema", "db_path", "secret.db", {}),
        ("json_query", "path", "secret.json", {"expression": "."}),
        ("csv_preview", "path", "secret.csv", {}),
    ],
)
@pytest.mark.bwrap
async def test_path_escape_rejected(connect, workspace, outside, tool, arg, name, extra):
    async with connect("data") as client:
        for path in _escape_paths(workspace, outside, name):
            r = await client.call_tool(tool, {arg: path, **extra})
            assert r.is_error, (tool, path, text(r))
            assert "outside the workspace" in text(r), (tool, path, text(r))
    assert sqlite3.connect(outside / "secret.db").execute("SELECT count(*) FROM users").fetchone()[0] == 2


# --- review regressions -----------------------------------------------------------


async def test_sqlite_query_invalid_utf8_text_does_not_fail_query(connect, workspace):
    conn = sqlite3.connect(workspace / "bad.db")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (CAST(x'61ff62' AS TEXT))")
    conn.commit()
    conn.close()
    async with connect("data") as client:
        r = await client.call_tool("sqlite_query", {"db_path": "bad.db", "sql": "SELECT x FROM t"})
    assert not r.is_error, text(r)
    assert "a�b" in text(r)


async def test_sqlite_query_out_of_range_param_is_actionable(connect, db):
    async with connect("data") as client:
        r = await client.call_tool("sqlite_query", {"db_path": "app.db", "sql": "SELECT ?", "params": [2**70]})
    assert r.is_error
    assert "64-bit" in text(r)


async def test_sqlite_schema_survives_broken_view(connect, workspace):
    conn = sqlite3.connect(workspace / "broken.db")
    conn.executescript("CREATE TABLE a (x); CREATE TABLE z (y INTEGER); CREATE VIEW v AS SELECT x FROM a;")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("DROP TABLE a")
    conn.close()
    async with connect("data") as client:
        r = await client.call_tool("sqlite_schema", {"db_path": "broken.db"})
    assert not r.is_error, text(r)
    out = text(r)
    assert "view v" in out and "columns unavailable" in out
    assert "table z" in out and "INTEGER" in out


async def test_csv_preview_quote_heavy_file_is_fast(connect, workspace):
    # csv.Sniffer is quadratic on this shape; a full 64 KiB sample took about 20 seconds.
    (workspace / "quotes.csv").write_text(',"' * 40_000)
    started = time.monotonic()
    async with connect("data") as client:
        r = await client.call_tool("csv_preview", {"path": "quotes.csv", "rows": 0})
    assert time.monotonic() - started < 5
    assert "columns:" in text(r) or r.is_error


async def test_csv_preview_wide_file_scans_fewer_rows(connect, workspace, monkeypatch):
    from dev_link_mcp.tools import data

    monkeypatch.setattr(data, "_CSV_SCAN_CELLS", 100)
    header = ",".join(f"c{i}" for i in range(10))
    (workspace / "wide.csv").write_text(header + "\n" + "".join(",".join("1" * 10) + "\n" for _ in range(50)))
    async with connect("data") as client:
        r = await client.call_tool("csv_preview", {"path": "wide.csv", "rows": 0})
    assert "data rows: first 10 rows (file has more)" in text(r), text(r)
