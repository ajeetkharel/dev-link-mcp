"""SQLite, JSON and CSV inspection tools."""

from __future__ import annotations

import asyncio
import csv
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import DESTRUCTIVE, READ_ONLY, clip

_MAX_CELL = 200
# Rows past max_rows are counted up to this many, so the agent knows the result size
# without a huge result set eating the time limit.
_COUNT_EXTRA = 100_000
# CSV statistics stop after this many rows, or this many cells for wide files, so a
# multi-gigabyte file cannot tie up a worker thread for minutes.
_CSV_SCAN_ROWS = 100_000
_CSV_SCAN_CELLS = 2_000_000
# csv.Sniffer is quadratic on quote-heavy input (about 20s for 64 KiB), so it only sees the start.
_SNIFF_CHARS = 8192
_CSV_NULLS = frozenset({"", "na", "n/a", "null", "none", "nan"})
_INT = re.compile(r"[+-]?\d+")
_FLOAT = re.compile(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?")
_BOOL = frozenset({"true", "false", "yes", "no", "t", "f", "y", "n"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")
# Narrowest first: a column's type is the first one every non-null value matches.
_TYPE_ORDER = ("integer", "float", "boolean", "date", "datetime", "string")


def _cell(value: Any) -> str:
    """Single-line display form of a value, with long values cut."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    s = str(value).replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return s if len(s) <= _MAX_CELL else s[: _MAX_CELL - 3] + "..."


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Aligned pipe-separated text table."""
    head = [_cell(h) for h in headers]
    body = [[_cell(v) for v in row] + [""] * (len(head) - len(row)) for row in rows]
    widths = [len(h) for h in head]
    for row in body:
        for i, value in enumerate(row[: len(widths)]):
            widths[i] = max(widths[i], len(value))
    lines = [" | ".join(h.ljust(w) for h, w in zip(head, widths, strict=True)).rstrip()]
    lines.append("-+-".join("-" * w for w in widths))
    for row in body:
        lines.append(" | ".join(v.ljust(w) for v, w in zip(row, widths, strict=False)).rstrip())
    return "\n".join(lines)


def _deny_attach(action: int, *_: Any) -> int:
    # ATTACH and VACUUM INTO can create or open files anywhere on the host, bypassing
    # the workspace check done on db_path.
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _open_db(path: Path, write: bool) -> sqlite3.Connection:
    """Open a SQLite file confined to itself: no ATTACH, read-only unless `write`.

    In write mode a missing file is created. Raises ToolError when SQLite cannot open it.
    """
    mode = "rwc" if write else "ro"
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=5, check_same_thread=False)
    except sqlite3.Error as exc:
        raise ToolError(f"cannot open database: {exc}") from exc
    # One bad byte in a TEXT value would otherwise fail the whole query.
    conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    conn.set_authorizer(_deny_attach)
    if not write:
        conn.execute("PRAGMA query_only = ON")
    return conn


async def _run_db(timeout_s: float, work: Callable[[Callable[[], bool]], str]) -> str:
    """Run blocking SQLite work in a thread, aborting it on timeout or cancellation.

    `work` receives a `stopped()` predicate to install as the connection's progress
    handler; SQLite then fails the running statement with "interrupted".
    """
    stop = threading.Event()
    deadline = time.monotonic() + timeout_s

    def stopped() -> bool:
        return stop.is_set() or time.monotonic() > deadline

    try:
        return await asyncio.to_thread(work, stopped)
    except asyncio.CancelledError:
        stop.set()
        raise


def _sql_error(exc: sqlite3.Error, timeout_s: float) -> ToolError:
    message = str(exc)
    if message == "interrupted":
        return ToolError(f"query stopped after the {timeout_s:.0f}s time limit; add a LIMIT or raise timeout_s")
    if "one statement at a time" in message:
        return ToolError("sql must be a single statement; send multiple statements as separate calls")
    if message == "not authorized" or "authorization denied" in message:
        return ToolError("ATTACH, DETACH and VACUUM INTO are not allowed; query one database file per call")
    if "readonly" in message or "read-only" in message:
        return ToolError(f"{message}; pass write=true to modify the database")
    return ToolError(f"SQLite error: {message}")


def _check_text_file(path: Path) -> None:
    if not path.is_file():
        raise ToolError(f"not a file: {path.name}")
    with path.open("rb") as fh:
        if b"\0" in fh.read(8192):
            raise ToolError(f"{path.name} looks binary; use file_info to inspect it")


def _value_type(value: str) -> str:
    if _INT.fullmatch(value):
        return "integer"
    if _FLOAT.fullmatch(value):
        return "float"
    if value.lower() in _BOOL:
        return "boolean"
    if _DATE.fullmatch(value):
        return "date"
    if _DATETIME.fullmatch(value):
        return "datetime"
    return "string"


def _merge_type(current: str | None, new: str) -> str:
    if current is None or current == new:
        return new
    numeric = {"integer", "float"}
    if current in numeric and new in numeric:
        return "float"
    if {current, new} == {"date", "datetime"}:
        return "datetime"
    return "string"


def _parse_delimiter(delimiter: str | None, sample: str, suffix: str) -> str:
    if delimiter is not None:
        if delimiter in ("\\t", "tab"):
            return "\t"
        if len(delimiter) != 1:
            raise ToolError("delimiter must be a single character (use '\\t' for tab)")
        return delimiter
    if suffix in (".tsv", ".tab"):
        return "\t"
    try:
        return csv.Sniffer().sniff(sample[:_SNIFF_CHARS], delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def _csv_preview(path: Path, display: str, rows: int, delimiter: str | None, has_header: bool) -> str:
    _check_text_file(path)
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(65536)
        fh.seek(0)
        delim = _parse_delimiter(delimiter, sample, path.suffix.lower())
        reader = csv.reader(fh, delimiter=delim)
        try:
            header = next(reader, None)
            if header is None:
                raise ToolError(f"{display} is empty")
            if not has_header:
                first = header
                header = [f"column_{i + 1}" for i in range(len(first))]
            width = len(header)
            scan_rows = min(_CSV_SCAN_ROWS, max(1, _CSV_SCAN_CELLS // max(width, 1)))
            types: list[str | None] = [None] * width
            nulls = [0] * width
            preview: list[list[str]] = []
            scanned = 0
            ragged = 0
            capped = False

            def take(record: list[str]) -> None:
                nonlocal ragged
                if len(record) != width:
                    ragged += 1
                for i in range(width):
                    value = record[i] if i < len(record) else ""
                    if value.strip().lower() in _CSV_NULLS:
                        nulls[i] += 1
                    else:
                        types[i] = _merge_type(types[i], _value_type(value.strip()))
                if len(preview) < rows:
                    preview.append(record)

            if not has_header:
                take(first)
                scanned = 1
            for record in reader:
                if not record:
                    continue
                if scanned >= scan_rows:
                    capped = True
                    break
                take(record)
                scanned += 1
        except csv.Error as exc:
            raise ToolError(f"cannot parse {display} at line {reader.line_num}: {exc}") from exc

    shown_delim = "\\t" if delim == "\t" else delim
    scope = f"first {scanned} rows (file has more)" if capped else f"{scanned} rows"
    lines = [f"file: {display}", f"delimiter: {shown_delim!r}; data rows: {scope}; columns: {width}"]
    if not has_header:
        lines.append("no header row: columns are named column_1..column_N")
    if ragged:
        lines.append(f"warning: {ragged} rows have a field count different from the header ({width})")
    lines += ["", "columns (types and null counts over the scanned rows):"]
    summary = [[name, t or "empty", n] for name, t, n in zip(header, types, nulls, strict=True)]
    lines.append(_table(["column", "type", "nulls"], summary))
    if rows:
        lines += ["", f"first {len(preview)} rows:", _table(header, preview)]
    return "\n".join(lines)


def _register(mcp: MCPServer, rt: Runtime) -> None:
    limits = rt.config.limits

    def db_file(db_path: str, *, create: bool) -> Path:
        path = rt.workspace.resolve(db_path, must_exist=not create)
        if path.exists() and not path.is_file():
            raise ToolError(f"not a file: {rt.workspace.rel(path)}")
        if not path.exists() and not path.parent.is_dir():
            raise ToolError(f"parent directory does not exist: {rt.workspace.rel(path.parent)}")
        return path

    @mcp.tool(annotations=DESTRUCTIVE)
    async def sqlite_query(
        db_path: Annotated[str, Field(description="SQLite database file, relative to the workspace root")],
        sql: Annotated[str, Field(description="One SQL statement. Use ? or :name placeholders for params.")],
        params: Annotated[
            list[Any] | dict[str, Any] | None,
            Field(description="Values for ? placeholders (list) or :name placeholders (object)"),
        ] = None,
        max_rows: Annotated[int, Field(ge=1, le=10_000, description="Rows to return; the rest are counted")] = 200,
        write: Annotated[
            bool,
            Field(description="Open read-write and commit. Needed for INSERT/UPDATE/DELETE/DDL; creates the file"),
        ] = False,
        timeout_s: Annotated[
            float | None, Field(description="Statement time limit in seconds; same default and maximum as run_command")
        ] = None,
    ) -> str:
        """Run one SQL statement against a SQLite file and return a text table with the row count.
        The database opens read-only unless write=true, which commits the statement and creates a
        missing file. ATTACH and VACUUM INTO are blocked."""
        path = db_file(db_path, create=write)
        timeout = rt.sandbox.clamp_timeout(timeout_s)

        def work(stopped: Callable[[], bool]) -> str:
            conn = _open_db(path, write)
            try:
                conn.set_progress_handler(stopped, 10_000)
                cur = conn.execute(sql, params if params is not None else ())
                if cur.description is None:
                    conn.commit()
                    changed = f"{cur.rowcount} rows affected" if cur.rowcount >= 0 else "statement executed"
                    return f"{changed} (committed)" if write else changed
                headers = [d[0] for d in cur.description]
                fetched = cur.fetchmany(max_rows)
                extra = 0
                while extra < _COUNT_EXTRA and (chunk := cur.fetchmany(1000)):
                    extra += len(chunk)
                more = extra >= _COUNT_EXTRA and cur.fetchone() is not None
                if write:
                    conn.commit()
            except sqlite3.Error as exc:
                raise _sql_error(exc, timeout) from exc
            except OverflowError as exc:
                raise ToolError(f"parameter out of range: {exc} (SQLite integers are 64-bit)") from exc
            finally:
                conn.close()
            total = f"{len(fetched) + extra}{'+' if more else ''}"
            footer = f"({total} rows)"
            if extra:
                footer = f"({total} rows, showing first {len(fetched)}; raise max_rows or add LIMIT/OFFSET)"
            return clip(f"{_table(headers, fetched)}\n{footer}", limits.max_output_bytes)

        return await _run_db(timeout, work)

    @mcp.tool(annotations=READ_ONLY)
    async def sqlite_schema(
        db_path: Annotated[str, Field(description="SQLite database file, relative to the workspace root")],
    ) -> str:
        """List every table and view in a SQLite file with its columns (type, NOT NULL, default,
        primary key), plus the CREATE statements for indexes and triggers. Read-only."""
        path = db_file(db_path, create=False)
        timeout = rt.sandbox.clamp_timeout(None)

        def work(stopped: Callable[[], bool]) -> str:
            conn = _open_db(path, write=False)
            try:
                conn.set_progress_handler(stopped, 10_000)
                objects = conn.execute(
                    "SELECT type, name, sql FROM sqlite_schema"
                    " WHERE name NOT LIKE 'sqlite_%' ORDER BY type = 'table' DESC, type, name"
                ).fetchall()
                out: list[str] = []
                for kind, name, _sql in objects:
                    if kind not in ("table", "view"):
                        continue
                    out.append(f"{kind} {name}")
                    try:
                        cols = conn.execute("SELECT * FROM pragma_table_info(?)", (name,)).fetchall()
                    except sqlite3.OperationalError as exc:
                        if str(exc) == "interrupted":
                            raise
                        # A virtual table whose module is not loaded, or a view over a dropped
                        # table, should not hide the rest of the schema.
                        out += [f"(columns unavailable: {exc})", ""]
                        continue
                    rows = [[c[1], c[2] or "", "yes" if c[3] else "", c[4], c[5] or ""] for c in cols]
                    out.append(_table(["column", "type", "not null", "default", "pk"], rows))
                    out.append("")
                extras = [s for k, _, s in objects if k not in ("table", "view") and s]
                if extras:
                    out.append("indexes and triggers:")
                    out += [f"{s};" for s in extras]
            except sqlite3.Error as exc:
                raise _sql_error(exc, timeout) from exc
            finally:
                conn.close()
            if not objects:
                return f"{rt.workspace.rel(path)}: no tables or views"
            return clip("\n".join(out).rstrip(), limits.max_output_bytes)

        return await _run_db(timeout, work)

    @mcp.tool(annotations=READ_ONLY)
    async def json_query(
        path: Annotated[str, Field(description="JSON or JSON Lines file, relative to the workspace root")],
        expression: Annotated[str, Field(description="jq filter, for example '.items[] | {id, name}'")] = ".",
        raw_output: Annotated[bool, Field(description="Print strings without quotes (jq -r)")] = False,
    ) -> str:
        """Run a jq filter over a JSON file and return jq's output. Use it to pull fields out of
        large JSON files instead of reading them whole. Output is clipped at the output cap."""
        target = rt.workspace.resolve(path, must_exist=True)
        if not target.is_file():
            raise ToolError(f"not a file: {rt.workspace.rel(target)}")
        argv = ["jq"]
        if raw_output:
            argv.append("-r")
        # "--" stops an expression that starts with "-" from being read as a jq option.
        argv += ["--", expression, str(target)]
        result = await rt.sandbox.run(argv, cwd=target.parent)
        if result.timed_out:
            raise ToolError(f"jq timed out after {result.duration_s:.0f}s")
        if result.exit_code == 127 or "command not found" in result.stderr:
            raise ToolError("jq is not installed in the sandbox")
        if not result.ok:
            raise ToolError(f"jq failed (exit {result.exit_code}): {result.stderr.strip() or result.stdout.strip()}")
        out = result.stdout.rstrip("\n")
        if result.truncated:
            out += "\n(output truncated: narrow the filter, e.g. '.items[:50]' or 'length')"
        return out or "(no output)"

    @mcp.tool(annotations=READ_ONLY)
    async def csv_preview(
        path: Annotated[str, Field(description="CSV/TSV file, relative to the workspace root")],
        rows: Annotated[int, Field(ge=0, le=1000, description="Data rows to show (0 for the summary only)")] = 20,
        delimiter: Annotated[
            str | None, Field(description="Field separator, e.g. ',', ';', '|' or '\\t'. Detected when omitted")
        ] = None,
        has_header: Annotated[bool, Field(description="Treat the first row as column names")] = True,
    ) -> str:
        """Summarize a delimited text file: delimiter, row count, each column's inferred type
        (integer, float, boolean, date, datetime, string) and null count, then the first rows as
        a table. Empty, NA, N/A, null, None and NaN count as null. Statistics cover up to the
        first 100000 rows (fewer for very wide files: about 2 million cells)."""
        target = rt.workspace.resolve(path, must_exist=True)
        report = await asyncio.to_thread(_csv_preview, target, rt.workspace.rel(target), rows, delimiter, has_header)
        return clip(report, limits.max_output_bytes)


class DataGroup(ToolGroup):
    """SQLite, JSON and CSV inspection tools."""

    name = "data"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)
