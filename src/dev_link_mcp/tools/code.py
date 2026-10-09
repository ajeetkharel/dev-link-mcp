"""Code intelligence: outlines, symbol definitions, references and line statistics.

Parsing uses tree-sitter-language-pack on the host. That only reads files (after each
path is checked against the workspace); every external program (rg) runs in the sandbox.
Grammars are fetched into the pack's cache the first time a language is used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import READ_ONLY, clip

# Dependency, build and cache dirs skipped even when no .gitignore lists them.
_VENDOR_DIRS = (
    ".git",
    "node_modules",
    "vendor",
    "bower_components",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "target",
    ".gradle",
    ".next",
    ".nuxt",
    ".cache",
)

# The pack maps .txt to vimdoc, which mislabels ordinary text files.
_NOT_CODE_SUFFIXES = frozenset({".txt"})

# Symbol kinds worth adding to an outline when the structure pass missed them
# (type aliases, Go type specs). Variables are left out to keep outlines short.
_OUTLINE_SYMBOL_KINDS = frozenset({"class", "enum", "interface", "type", "function", "module"})

# Containers whose function children are methods. The pack labels Python and Rust methods
# as plain functions, which would make kind="method" miss them.
_METHOD_PARENTS = frozenset({"class", "struct", "impl", "trait", "interface"})

SymbolKindName = Literal[
    "class", "function", "method", "interface", "struct", "enum", "trait", "impl", "module", "namespace", "type"
]

# rg output for file listings can be large; the default output cap would elide the middle.
_LIST_OUTPUT_BYTES = 16_000_000
_MAX_CANDIDATE_FILES = 2000
_BINARY_PROBE_BYTES = 8192


@dataclass
class _Item:
    """One outline entry. Lines are 1-based and inclusive."""

    kind: str
    name: str
    start: int
    end: int
    children: list[_Item] = field(default_factory=list)


def _kind_name(kind: object) -> str:
    return str(kind).lower()


def _language_for(path: Path, head: bytes) -> str | None:
    """Language name for a file from its name, falling back to a shebang line."""
    import tree_sitter_language_pack as tslp

    if path.suffix.lower() in _NOT_CODE_SUFFIXES:
        return None
    language = tslp.detect_language(path.name)
    if language is None and head.startswith(b"#!"):
        first_line = head.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        language = tslp.detect_language_from_content(first_line)
    return language


def _is_binary(head: bytes) -> bool:
    return b"\0" in head[:_BINARY_PROBE_BYTES]


def _convert(item: Any) -> _Item:
    span = item.span
    return _Item(
        kind=_kind_name(item.kind),
        name=item.name or "<anonymous>",
        start=span.start_line + 1 if span else 0,
        end=span.end_line + 1 if span else 0,
        children=[_convert(child) for child in item.children],
    )


def _insert(items: list[_Item], new: _Item) -> None:
    """Place `new` under the innermost item whose line range contains it, keeping line order."""
    for item in items:
        if item.start <= new.start and new.end <= item.end and (item.start, item.end) != (new.start, new.end):
            _insert(item.children, new)
            return
    items.append(new)
    items.sort(key=lambda i: i.start)


def _label_methods(items: list[_Item], parent_kind: str | None = None) -> None:
    for item in items:
        if item.kind == "function" and parent_kind in _METHOD_PARENTS:
            item.kind = "method"
        _label_methods(item.children, item.kind)


def _walk(items: list[_Item], parent: str | None = None) -> list[tuple[_Item, str | None]]:
    out: list[tuple[_Item, str | None]] = []
    for item in items:
        out.append((item, parent))
        out.extend(_walk(item.children, item.name))
    return out


def _outline(source: str, language: str) -> list[_Item]:
    """Structure tree for `source`, topped up with type-like symbols the structure pass skips.

    Raises tree_sitter_language_pack.Error when the grammar cannot be loaded or downloaded.
    """
    import tree_sitter_language_pack as tslp

    config = tslp.ProcessConfig(language=language, structure=True, imports=False, exports=False, symbols=True)
    result = tslp.process(source, config)
    items = [_convert(item) for item in result.structure]
    seen = {(i.name, i.start) for i, _ in _walk(items)}
    for symbol in result.symbols:
        kind = _kind_name(symbol.kind)
        if kind not in _OUTLINE_SYMBOL_KINDS or symbol.span is None or not symbol.name:
            continue
        start = symbol.span.start_line + 1
        if (symbol.name, start) in seen:
            continue
        seen.add((symbol.name, start))
        _insert(items, _Item(kind=kind, name=symbol.name, start=start, end=symbol.span.end_line + 1))
    _label_methods(items)
    return items


def _format_outline(items: list[_Item], depth: int = 0) -> list[str]:
    lines: list[str] = []
    for item in items:
        span = f"L{item.start}" if item.start == item.end else f"L{item.start}-{item.end}"
        lines.append(f"{'  ' * depth}{item.kind} {item.name}  {span}")
        lines.extend(_format_outline(item.children, depth + 1))
    return lines


def _kind_matches(wanted: str | None, kind: str) -> bool:
    if wanted is None:
        return True
    # Agents ask for "function" when they mean methods too; "method" stays narrow.
    if wanted == "function":
        return kind in ("function", "method")
    return kind == wanted


def _validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ToolError("name must not be empty")
    if "\0" in name:
        raise ToolError("name must not contain a NUL byte")
    if any(ch.isspace() for ch in name):
        raise ToolError("name must be a single identifier without whitespace")
    if len(name) > 200:
        raise ToolError("name is too long (max 200 characters)")
    return name


def _register(mcp: MCPServer, rt: Runtime) -> None:
    # Imported here, not at module level, so this module loads without the `code` extra; the
    # registry only registers the group when the package is installed.
    import tree_sitter_language_pack as tslp

    limits = rt.config.limits

    def read_source(target: Path) -> tuple[str, str | None]:
        """Text and language of a workspace file. Raises ToolError for binary or oversized files."""
        size = target.stat().st_size
        if size > limits.max_read_bytes:
            raise ToolError(
                f"{rt.workspace.rel(target)} is {size} bytes, over the {limits.max_read_bytes}-byte read limit"
            )
        data = target.read_bytes()
        if _is_binary(data):
            raise ToolError(f"{rt.workspace.rel(target)} is a binary file; use file_info")
        return data.decode("utf-8", errors="replace"), _language_for(target, data[:256])

    def rg_scope(target: Path) -> tuple[list[str], list[str]]:
        """rg path argument and vendor-dir excludes for a search rooted at `target`.

        Excludes are dropped for vendor dirs the target itself is inside, so an explicit
        path like node_modules/pkg still gets searched.
        """
        rel = rt.workspace.rel(target)
        parts = set(Path(rel).parts)
        globs: list[str] = []
        for name in _VENDOR_DIRS:
            if name not in parts:
                globs += ["-g", f"!{name}/"]
        return ([] if rel == "." else [rel]), globs

    async def rg(args: list[str], *, max_output_bytes: int | None = None) -> tuple[str, bool]:
        """Run rg from the workspace root. Returns (stdout, truncated); no matches gives ''."""
        # --no-messages hides per-file errors (unreadable files, dangling symlinks) and the
        # "no files were searched" warning, which also exit 2; usage errors still print.
        result = await rt.sandbox.run(
            ["rg", "--no-require-git", "--no-messages", "--color=never", *args],
            cwd=rt.workspace.root,
            max_output_bytes=max_output_bytes,
        )
        if result.timed_out:
            raise ToolError(f"search timed out after {result.duration_s:.0f}s; narrow the path")
        if result.exit_code not in (0, 1) and not result.stdout and result.stderr.strip():
            raise ToolError(f"rg failed: {result.stderr.strip()}")
        return result.stdout, result.truncated

    def listed_files(stdout: str) -> list[Path]:
        """Resolve rg file-list output, dropping elision markers and anything outside the workspace."""
        files: list[Path] = []
        for line in stdout.split("\n"):
            if not line:
                continue
            try:
                path = rt.workspace.resolve(line)
            except ToolError:
                continue
            if path.is_file():
                files.append(path)
        return files

    @mcp.tool(annotations=READ_ONLY)
    async def code_outline(
        path: Annotated[str, Field(description="Source file, relative to the workspace root")],
    ) -> str:
        """List the classes, functions, methods, interfaces, structs, enums and types declared in one
        source file, nested by containment, with 1-based line ranges (`L12-40`). Parsed with
        tree-sitter, which covers most languages; the reply says when a file's language is not one."""
        target = rt.workspace.resolve(path, must_exist=True)
        if not target.is_file():
            raise ToolError(f"{rt.workspace.rel(target)} is not a file")
        source, language = await asyncio.to_thread(read_source, target)
        rel = rt.workspace.rel(target)
        if language is None:
            return f"{rel}: language not recognized, so no outline is available. Use read_file or grep instead."
        try:
            items = await asyncio.to_thread(_outline, source, language)
        except tslp.Error as exc:
            raise ToolError(f"no {language} parser available ({exc}); use read_file or grep instead") from exc
        total = source.count("\n") + (0 if source.endswith("\n") or not source else 1)
        header = f"{rel} ({language}, {total} lines)"
        if not items:
            return f"{header}\n(no declarations found)"
        return clip("\n".join([header, *_format_outline(items)]), limits.max_output_bytes)

    @mcp.tool(annotations=READ_ONLY)
    async def find_symbol(
        name: Annotated[str, Field(description="Exact symbol name, e.g. `UserService` or `parse_args`")],
        path: Annotated[str, Field(description="File or directory to search, relative to the workspace root")] = ".",
        kind: Annotated[
            SymbolKindName | None,
            Field(description="Only definitions of this kind; `function` also matches methods"),
        ] = None,
        max_results: Annotated[int, Field(ge=1, le=1000, description="Maximum definitions to return")] = 50,
    ) -> str:
        """Find where a symbol is defined (class, function, method, type, ...), not where it is used.
        Files that mention the name, minus .gitignore'd and vendor/build dirs, are parsed with
        tree-sitter. Returns `path:start-end kind name (in Parent)` lines."""
        name = _validate_name(name)
        target = rt.workspace.resolve(path, must_exist=True)
        scope, globs = rg_scope(target)
        stdout, truncated = await rg(
            ["--files-with-matches", "--word-regexp", "--fixed-strings", *globs, "--", name, *scope],
            max_output_bytes=_LIST_OUTPUT_BYTES,
        )
        candidates = sorted(await asyncio.to_thread(listed_files, stdout))
        notes: list[str] = []
        if truncated or len(candidates) > _MAX_CANDIDATE_FILES:
            notes.append(f"too many candidate files; only the first {_MAX_CANDIDATE_FILES} were parsed, narrow path")
        candidates = candidates[:_MAX_CANDIDATE_FILES]

        def scan() -> tuple[list[str], int, list[str]]:
            hits: list[str] = []
            unparsed = 0
            failed: set[str] = set()
            for file in candidates:
                try:
                    source, language = read_source(file)
                except (ToolError, OSError):
                    unparsed += 1
                    continue
                if language is None or language in failed:
                    unparsed += 1
                    continue
                try:
                    items = _outline(source, language)
                except tslp.Error:
                    failed.add(language)
                    unparsed += 1
                    continue
                for item, parent in _walk(items):
                    if item.name == name and _kind_matches(kind, item.kind):
                        where = f" (in {parent})" if parent else ""
                        hits.append(f"{rt.workspace.rel(file)}:{item.start}-{item.end} {item.kind} {item.name}{where}")
            return hits, unparsed, sorted(failed)

        hits, unparsed, failed = await asyncio.to_thread(scan)
        if unparsed:
            notes.append(f"{unparsed} file(s) mention the name but could not be parsed; try find_references")
        if failed:
            notes.append(f"parser unavailable for: {', '.join(failed)}")
        if not hits:
            lines = [f"no definition of {name!r} found under {rt.workspace.rel(target)}"]
        else:
            lines = hits[:max_results]
            if len(hits) > max_results:
                notes.append(f"{len(hits) - max_results} more definitions not shown; raise max_results")
        return clip("\n".join(lines + [f"note: {n}" for n in notes]), limits.max_output_bytes)

    @mcp.tool(annotations=READ_ONLY)
    async def find_references(
        name: Annotated[str, Field(description="Identifier to look for, matched as a whole word")],
        path: Annotated[str, Field(description="File or directory to search, relative to the workspace root")] = ".",
        ignore_case: Annotated[bool, Field(description="Case-insensitive match")] = False,
        max_results: Annotated[int, Field(ge=1, le=5000, description="Maximum matching lines to show")] = 200,
    ) -> str:
        """Find every line that mentions an identifier as a whole word (definitions, calls, imports,
        comments), grouped by file with line numbers. A text match, not semantic; skips .gitignore'd
        files and vendor/build dirs. Long lines are cut to 300 characters."""
        name = _validate_name(name)
        target = rt.workspace.resolve(path, must_exist=True)
        scope, globs = rg_scope(target)
        flags = ["--line-number", "--with-filename", "--no-heading", "--null", "--word-regexp", "--fixed-strings"]
        flags += ["--max-columns", "300", "--max-columns-preview"]
        if ignore_case:
            flags.append("--ignore-case")
        # Totals are counted from the raw output, so it gets the large cap; the final text is clipped.
        stdout, truncated = await rg([*flags, *globs, "--", name, *scope], max_output_bytes=_LIST_OUTPUT_BYTES)

        def group() -> tuple[dict[str, list[str]], int]:
            by_file: dict[str, list[str]] = {}
            total = 0
            # Split on newlines only: str.splitlines also breaks on form feeds and U+2028 inside lines.
            for line in stdout.split("\n"):
                file, sep, rest = line.partition("\0")
                if not sep:
                    continue
                lineno, _, content = rest.partition(":")
                total += 1
                by_file.setdefault(file.removeprefix("./"), []).append(f"  {lineno}: {content.strip()}")
            return by_file, total

        by_file, total = await asyncio.to_thread(group)
        if not by_file:
            return f"no references to {name!r} under {rt.workspace.rel(target)}"

        at_least = "at least " if truncated else ""
        out = [f"{at_least}{total} match(es) in {at_least}{len(by_file)} file(s)"]
        shown = 0
        for file in sorted(by_file):
            matches = by_file[file]
            if shown >= max_results:
                break
            out.append(f"{file} ({len(matches)})")
            take = matches[: max_results - shown]
            out.extend(take)
            shown += len(take)
        if shown < total:
            out.append(f"note: {total - shown} more match(es) not shown; raise max_results or narrow path")
        if truncated:
            out.append("note: search output was truncated; narrow path for complete results")
        return clip("\n".join(out), limits.max_output_bytes)

    @mcp.tool(annotations=READ_ONLY)
    async def code_stats(
        path: Annotated[str, Field(description="Directory or file, relative to the workspace root")] = ".",
    ) -> dict[str, Any]:
        """Count files and total/code/comment/blank lines per language, like cloc. Skips .gitignore'd
        and hidden files and vendor/build dirs (node_modules, .venv, dist, target, ...). Unrecognized,
        binary and oversized files are counted under `skipped`."""
        target = rt.workspace.resolve(path, must_exist=True)
        if target.is_file():
            files, truncated = [target], False
        else:
            scope, globs = rg_scope(target)
            stdout, truncated = await rg(["--files", *globs, *scope], max_output_bytes=_LIST_OUTPUT_BYTES)
            files = await asyncio.to_thread(listed_files, stdout)

        def count() -> dict[str, Any]:
            per_language: dict[str, dict[str, int]] = {}
            skipped = {"unrecognized": 0, "binary": 0, "too_large": 0, "unreadable": 0}
            failed: set[str] = set()
            for file in files:
                try:
                    if file.stat().st_size > limits.max_read_bytes:
                        skipped["too_large"] += 1
                        continue
                    data = file.read_bytes()
                except OSError:
                    skipped["unreadable"] += 1
                    continue
                if _is_binary(data):
                    skipped["binary"] += 1
                    continue
                language = _language_for(file, data[:256])
                if language is None:
                    skipped["unrecognized"] += 1
                    continue
                source = data.decode("utf-8", errors="replace")
                stats = per_language.setdefault(language, {"files": 0, "lines": 0, "code": 0, "comment": 0, "blank": 0})
                stats["files"] += 1
                metrics = None
                if language not in failed:
                    try:
                        config = tslp.ProcessConfig(language=language, structure=False, imports=False, exports=False)
                        metrics = tslp.process(source, config).metrics
                    except tslp.Error:
                        failed.add(language)
                if metrics is not None:
                    stats["lines"] += metrics.total_lines
                    stats["code"] += metrics.code_lines
                    stats["comment"] += metrics.comment_lines
                    stats["blank"] += metrics.blank_lines
                else:
                    # Without a grammar comments cannot be told apart from code.
                    lines = source.split("\n")
                    if lines[-1] == "":
                        lines.pop()
                    blank = sum(1 for line in lines if not line.strip())
                    stats["lines"] += len(lines)
                    stats["blank"] += blank
                    stats["code"] += len(lines) - blank
            languages = [
                {"language": lang, **stats}
                for lang, stats in sorted(per_language.items(), key=lambda kv: (-kv[1]["code"], kv[0]))
            ]
            totals = {
                key: sum(s[key] for s in per_language.values())
                for key in ("files", "lines", "code", "comment", "blank")
            }
            result: dict[str, Any] = {
                "path": rt.workspace.rel(target),
                "languages": languages,
                "totals": totals,
                "skipped": skipped,
            }
            if failed:
                result["no_parser"] = sorted(failed)
                result["note"] = "comment lines are counted as code for languages in no_parser"
            if truncated:
                result["truncated"] = True
                result["note_truncated"] = "file list was too long and was cut; run on a subdirectory"
            return result

        return await asyncio.to_thread(count)


class CodeGroup(ToolGroup):
    """Code intelligence: outlines, symbol definitions, references and line statistics."""

    name = "code"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)
