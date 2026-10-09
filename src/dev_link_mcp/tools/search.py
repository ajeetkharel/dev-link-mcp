"""Content and file-name search with ripgrep, plus search-and-replace across files."""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.sandbox import RunResult
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import READ_ONLY, WRITE, clip

# Long lines (minified JS, data blobs) would flood the agent's context.
_MAX_LINE_CHARS = 500
# rg output (especially --json) is much larger than the text we return; this only bounds memory.
_RG_CAPTURE_BYTES = 32_000_000
_MAX_RESULTS_CAP = 5000
_MAX_REPLACE_FILES = 20_000

_HIDDEN_FIELD = Field(description="Also search hidden files and directories (dot-files). .git is always skipped.")
_IGNORED_FIELD = Field(description="Also search files excluded by .gitignore/.ignore (node_modules, build output...).")


def _filter_flags(globs: list[str], include_hidden: bool, include_ignored: bool) -> list[str]:
    """rg flags for the agent's globs plus hidden/ignored handling.

    A matching --glob whitelists a file even if it is hidden, so without include_hidden we add
    '!.*' after the agent's globs (the last matching glob wins) to keep dot-files out.
    """
    flags = [*globs, "--glob", "!.git"]
    if include_hidden:
        flags.insert(0, "--hidden")
    else:
        flags += ["--glob", "!.*"]
    if include_ignored:
        flags.append("--no-ignore")
    return flags


def _decode(value: dict[str, Any]) -> str:
    """Text of an rg JSON `{"text": ...}` or `{"bytes": base64}` value."""
    if "text" in value:
        return value["text"]
    return base64.b64decode(value.get("bytes", "")).decode("utf-8", errors="replace")


def _short(line: str) -> str:
    line = line.rstrip("\r\n")
    if len(line) > _MAX_LINE_CHARS:
        return line[:_MAX_LINE_CHARS] + f" ... [{len(line) - _MAX_LINE_CHARS} more characters]"
    return line


@dataclass
class _FileHits:
    # (line_number, is_match, text) in rg's order, which is ascending line number per file.
    lines: list[tuple[int, bool, str]] = field(default_factory=list)


def _parse_rg_json(stdout: str) -> dict[str, _FileHits]:
    hits: dict[str, _FileHits] = {}
    for raw in stdout.splitlines():
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            # A line cut by the output cap; the caller reports the result as incomplete.
            continue
        kind = event.get("type")
        if kind not in ("match", "context"):
            continue
        data = event["data"]
        path = _decode(data["path"])
        number = data.get("line_number") or 0
        hits.setdefault(path, _FileHits()).lines.append((number, kind == "match", _short(_decode(data["lines"]))))
    return hits


def _format_content(hits: dict[str, _FileHits], max_results: int, context: int) -> tuple[str, int, int, bool]:
    """Render matches grouped by file. Returns (text, matches shown, total matches, truncated).

    The total is exact only when not truncated: rg stops each file one match past max_results.
    """
    total = sum(1 for h in hits.values() for _, is_match, _ in h.lines if is_match)
    blocks: list[str] = []
    shown = 0
    truncated = False
    for path in sorted(hits):
        if truncated:
            break
        out = [path]
        previous = None
        since_match = 0
        for number, is_match, line in hits[path].lines:
            if is_match:
                if shown >= max_results:
                    truncated = True
                    break
                shown += 1
                since_match = 0
            else:
                since_match += 1
                # Leading context of a match we are about to cut off.
                if shown >= max_results and since_match > context:
                    continue
            if previous is not None and number > previous + 1:
                out.append("--")
            out.append(f"{number}{':' if is_match else '-'} {line}")
            previous = number
        if len(out) > 1:
            blocks.append("\n".join(out))
    return "\n\n".join(blocks), shown, total, truncated


def _split_null(stdout: str) -> list[str]:
    return [p for p in stdout.split("\0") if p.strip("\n")]


def _check_rg(result: RunResult, has_results: bool) -> str:
    """Raise for failed searches; return a warning suffix when rg finished but hit per-file errors."""
    if result.timed_out:
        raise ToolError(
            f"search timed out after {result.duration_s:.0f}s; narrow it with a smaller path or a glob filter"
        )
    if result.exit_code in (0, 1):
        return ""
    raw = result.stderr.strip()
    stderr = clip(raw, 2000)
    # rg exits 2 both for fatal errors (bad regex or glob) and for files it could not read during an
    # otherwise complete search; only the per-file kind carries an OS error code on every line.
    per_file = bool(raw) and all("(os error" in line for line in raw.splitlines())
    if not has_results and not per_file:
        hint = ""
        if "regex parse error" in stderr:
            hint = "\nThe pattern is a Rust regex; set fixed_strings=true to search for literal text."
        raise ToolError(f"rg failed (exit {result.exit_code}): {stderr}{hint}")
    return f"\n\nwarnings from rg:\n{stderr}" if stderr else ""


@dataclass
class _Replacement:
    rel: str
    path: Path
    old: str
    new: str
    count: int


def _diff(rel: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{rel}", tofile=f"b/{rel}", n=2
    )
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def _write_atomic(path: Path, content: str) -> None:
    """Replace the file in one step so a crash never leaves it half-written; keeps its permissions."""
    mode = path.stat().st_mode
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
        os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _register(mcp: MCPServer, rt: Runtime) -> None:
    def require_rg() -> None:
        if shutil.which("rg", path=rt.sandbox.path) is None:
            raise ToolError("ripgrep (rg) is not installed; install it (apt install ripgrep) to use search tools")

    def target(path: str, *, directory: bool = False) -> list[str]:
        """rg path argument for an agent path: empty for the workspace root so output paths stay clean."""
        resolved = rt.workspace.resolve(path, must_exist=True)
        if directory and not resolved.is_dir():
            raise ToolError(f"not a directory: {rt.workspace.rel(resolved)}")
        rel = rt.workspace.rel(resolved)
        return [] if rel == "." else [rel]

    async def rg(args: list[str]) -> RunResult:
        require_rg()
        # Run from the root so rg prints workspace-relative paths and globs with '/' are root-relative.
        return await rt.sandbox.run(
            ["rg", "--no-config", "--color", "never", *args],
            cwd=rt.workspace.root,
            max_output_bytes=_RG_CAPTURE_BYTES,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def grep(
        pattern: Annotated[str, Field(description="Rust regex (ripgrep syntax), or literal text with fixed_strings.")],
        path: Annotated[str, Field(description="File or directory to search, relative to the workspace root.")] = ".",
        glob: Annotated[
            str | None,
            Field(description="Only search files matching this glob, e.g. '*.py' or 'src/**/*.ts'; '!x' excludes."),
        ] = None,
        ignore_case: Annotated[bool, Field(description="Case-insensitive matching.")] = False,
        fixed_strings: Annotated[bool, Field(description="Treat pattern as literal text, not a regex.")] = False,
        context: Annotated[int, Field(ge=0, le=20, description="Lines of context before and after each match.")] = 0,
        output_mode: Annotated[
            Literal["content", "files", "count"],
            Field(description="content: matching lines; files: file paths only; count: matching lines per file."),
        ] = "content",
        max_results: Annotated[
            int,
            Field(ge=1, le=_MAX_RESULTS_CAP, description="Max matching lines (content) or files (files/count)."),
        ] = 200,
        include_hidden: Annotated[bool, _HIDDEN_FIELD] = False,
        include_ignored: Annotated[bool, _IGNORED_FIELD] = False,
    ) -> str:
        """Search file contents with ripgrep, skipping .gitignore'd, hidden and binary files by
        default. content mode groups matches by file as `line: text` (context lines `line- text`);
        files mode lists paths; count mode prints `path: count`. Sorted by path and capped at
        max_results; the footer says when more exist."""
        if not pattern:
            raise ToolError("pattern must not be empty")
        args = _filter_flags(["--glob", glob] if glob else [], include_hidden, include_ignored)
        if ignore_case:
            args.append("--ignore-case")
        if fixed_strings:
            args.append("--fixed-strings")
        paths = target(path)
        limit = rt.config.limits.max_output_bytes

        if output_mode == "content":
            # One match over the cap per file lets _format_content tell "exactly max_results" from "more exist".
            args += ["--json", "--context", str(context), "--max-count", str(max_results + 1)]
            result = await rg([*args, "--regexp", pattern, "--", *paths])
            # Parsing up to _RG_CAPTURE_BYTES of JSON takes long enough to stall other requests.
            hits = await asyncio.to_thread(_parse_rg_json, result.stdout)
            warning = _check_rg(result, bool(hits))
            if not hits:
                return f"no matches for {pattern!r}{warning}"
            body, shown, total, truncated = _format_content(hits, max_results, context)
            files_hit = sum(1 for h in hits.values() if any(is_match for _, is_match, _ in h.lines))
            footer = f"{total} matching lines in {files_hit} files"
            if truncated:
                footer = (
                    f"showing the first {shown} matching lines; more exist in {files_hit} files"
                    " (raise max_results or narrow path/glob)"
                )
            if result.truncated:
                footer += " (result set too large: some matches were dropped; narrow the search)"
            return clip(f"{body}\n\n{footer}{warning}", limit)

        flag = "--files-with-matches" if output_mode == "files" else "--count"
        # --with-filename: for a single file argument rg would otherwise print a bare count.
        result = await rg([*args, flag, "--with-filename", "--null", "--regexp", pattern, "--", *paths])
        if output_mode == "files":
            entries = sorted(p.strip("\n") for p in _split_null(result.stdout))
        else:
            # --null --count prints `path\0count\n`.
            pairs = [line.split("\0", 1) for line in result.stdout.splitlines() if "\0" in line]
            entries = sorted(f"{p}: {c}" for p, c in pairs)
        warning = _check_rg(result, bool(entries))
        if not entries:
            return f"no matches for {pattern!r}{warning}"
        footer = f"{len(entries)} files"
        if len(entries) > max_results:
            footer = f"showing {max_results} of {len(entries)} files; raise max_results or narrow path/glob"
        if result.truncated:
            footer += " (output too large: some files were dropped; narrow the search)"
        return clip("\n".join(entries[:max_results]) + f"\n\n{footer}{warning}", limit)

    @mcp.tool(annotations=READ_ONLY)
    async def find_files(
        pattern: Annotated[
            str,
            Field(
                description="Glob for file paths. Without '/', it matches file names at any depth ('*.py', "
                "'Dockerfile*'); with '/', it matches workspace-relative paths ('src/**/test_*.py')."
            ),
        ],
        path: Annotated[str, Field(description="Directory to search, relative to the workspace root.")] = ".",
        max_results: Annotated[int, Field(ge=1, le=_MAX_RESULTS_CAP, description="Max paths to return.")] = 500,
        ignore_case: Annotated[bool, Field(description="Case-insensitive glob matching.")] = False,
        include_hidden: Annotated[bool, _HIDDEN_FIELD] = False,
        include_ignored: Annotated[bool, _IGNORED_FIELD] = False,
    ) -> str:
        """Find files by name or path glob (ripgrep --files). Respects .gitignore and skips hidden
        files by default. Returns workspace-relative paths sorted alphabetically, capped at max_results."""
        if not pattern.strip():
            raise ToolError("pattern must not be empty")
        args = _filter_flags(["--iglob" if ignore_case else "--glob", pattern], include_hidden, include_ignored)
        paths = target(path, directory=True)
        result = await rg(["--files", "--null", *args, "--", *paths])
        files = sorted(p.strip("\n") for p in _split_null(result.stdout))
        warning = _check_rg(result, bool(files))
        if not files:
            return f"no files match {pattern!r}{warning}"
        footer = f"{len(files)} files"
        if len(files) > max_results:
            footer = f"showing {max_results} of {len(files)} files; raise max_results or use a narrower pattern"
        return clip("\n".join(files[:max_results]) + f"\n\n{footer}{warning}", rt.config.limits.max_output_bytes)

    def plan_replacements(
        files: list[str], compiled: re.Pattern[str], replacement: str, literal: bool
    ) -> tuple[list[_Replacement], list[str]]:
        """Compute new contents for every file with a match. Returns (replacements, skipped notes)."""
        max_bytes = rt.config.limits.max_read_bytes
        planned: list[_Replacement] = []
        skipped: list[str] = []
        # A literal replacement must not interpret backslashes or group references.
        repl: Any = (lambda _m: replacement) if literal else replacement
        for rel in files:
            try:
                resolved = rt.workspace.resolve(rel, must_exist=True)
            except ToolError:
                skipped.append(f"{rel}: outside the workspace")
                continue
            if not resolved.is_file():
                continue
            try:
                if resolved.stat().st_size > max_bytes:
                    skipped.append(f"{rel}: larger than {max_bytes} bytes")
                    continue
                data = resolved.read_bytes()
            except OSError as exc:
                skipped.append(f"{rel}: {exc.strerror or exc}")
                continue
            if b"\0" in data[:8192]:
                continue
            try:
                old = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(f"{rel}: not UTF-8 text")
                continue
            try:
                new, count = compiled.subn(repl, old)
            except (re.error, IndexError) as exc:
                raise ToolError(f"invalid replacement {replacement!r}: {exc}") from exc
            if count and new != old:
                planned.append(_Replacement(rel=rel, path=resolved, old=old, new=new, count=count))
        return planned, skipped

    @mcp.tool(annotations=WRITE)
    async def replace_in_files(
        pattern: Annotated[str, Field(description="Python regex (re module), or literal text when regex=false.")],
        replacement: Annotated[
            str,
            Field(description=r"Replacement text. With regex=true, \1 or \g<name> insert captured groups."),
        ],
        glob: Annotated[
            str | None, Field(description="Only touch files matching this glob, e.g. '*.py' or 'src/**/*.ts'.")
        ] = None,
        path: Annotated[str, Field(description="File or directory, relative to the workspace root.")] = ".",
        regex: Annotated[bool, Field(description="Treat pattern as a regex; false means literal text.")] = True,
        ignore_case: Annotated[bool, Field(description="Case-insensitive matching.")] = False,
        dry_run: Annotated[bool, Field(description="Preview only. Set false to write the changes.")] = True,
        include_hidden: Annotated[bool, _HIDDEN_FIELD] = False,
        include_ignored: Annotated[bool, _IGNORED_FIELD] = False,
    ) -> str:
        """Search and replace across files, showing a unified diff per changed file. Defaults to a
        dry run; call again with dry_run=false to write. Skips .gitignore'd, hidden, binary, non-UTF-8
        and oversized files. Patterns can span lines; use (?m) for per-line ^ and $. Each file is
        replaced atomically."""
        if not pattern:
            raise ToolError("pattern must not be empty")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as exc:
            raise ToolError(f"invalid regex {pattern!r}: {exc}; set regex=false for literal text") from exc

        args = _filter_flags(["--glob", glob] if glob else [], include_hidden, include_ignored)
        result = await rg(["--files", "--null", *args, "--", *target(path)])
        files = sorted(p.strip("\n") for p in _split_null(result.stdout))
        warning = _check_rg(result, bool(files))
        if result.truncated:
            raise ToolError("too many files to scan; narrow it with a smaller path or a glob filter")
        if len(files) > _MAX_REPLACE_FILES:
            raise ToolError(f"{len(files)} files in scope (limit {_MAX_REPLACE_FILES}); narrow path or glob")

        planned, skipped = await asyncio.to_thread(plan_replacements, files, compiled, replacement, not regex)
        notes = ""
        if skipped:
            notes = "\n\nskipped:\n" + "\n".join(skipped[:50])
            if len(skipped) > 50:
                notes += f"\n... and {len(skipped) - 50} more"
        if not planned:
            return f"no matches for {pattern!r} in {len(files)} files{notes}{warning}"

        total = sum(r.count for r in planned)
        if dry_run:
            header = (
                f"DRY RUN: {total} replacements in {len(planned)} files would be made; nothing was written. "
                "Call again with dry_run=false to apply."
            )
        else:
            written: list[str] = []

            def write_all() -> None:
                for r in planned:
                    _write_atomic(r.path, r.new)
                    written.append(r.rel)

            try:
                await asyncio.to_thread(write_all)
            except OSError as exc:
                done = ", ".join(written) or "none"
                raise ToolError(f"write failed ({exc}); files already changed: {done}") from exc
            header = f"{total} replacements written in {len(planned)} files."
        summary = "\n".join(f"  {r.rel}: {r.count}" for r in planned)
        diffs = "\n".join(_diff(r.rel, r.old, r.new) for r in planned)
        return clip(f"{header}\n{summary}{notes}{warning}\n\n{diffs}", rt.config.limits.max_output_bytes)


class SearchGroup(ToolGroup):
    """Content and file-name search with ripgrep, plus search-and-replace across files."""

    name = "search"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)
