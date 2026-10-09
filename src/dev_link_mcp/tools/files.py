"""Reading, writing, editing and organising files inside the workspace."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import mimetypes
import os
import shlex
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypeVar

import mcp.types as types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import DESTRUCTIVE, READ_ONLY, WRITE, clip

_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# Lines longer than this are cut so one minified line cannot fill the whole response.
_MAX_LINE_CHARS = 2000
# Read size per readline call; keeps memory bounded on files with enormous lines.
_READ_CHUNK = 65536
_SNIFF_BYTES = 8192
_MAX_DIFF_CHARS = 20_000
# Generated, vendored or cached trees that drown out the source in a tree listing.
_TREE_SKIP = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".gradle",
        ".next",
        ".nuxt",
        ".cache",
        ".idea",
        ".terraform",
        "dist",
        "build",
        "target",
        ".eggs",
        "coverage",
        ".coverage",
        "htmlcov",
    }
)


class EditSpec(BaseModel):
    old_string: Annotated[str, Field(description="Exact text to find, including whitespace and indentation.")]
    new_string: Annotated[str, Field(description="Replacement text.")]
    replace_all: Annotated[bool, Field(description="Replace every occurrence instead of requiring exactly one.")] = (
        False
    )


def _is_binary(sample: bytes) -> bool:
    """True when the sample has NUL bytes or is mostly not UTF-8."""
    if b"\0" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        # Mostly-text files in a legacy encoding (or a sample cut mid-character) are still
        # readable with a few replacement characters; dense garbage is not.
        return sample.decode("utf-8", errors="replace").count("\ufffd") > len(sample) // 10
    return False


def _sniff(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read(_SNIFF_BYTES)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def _numbered_lines(path: Path, offset: int, limit: int, budget: int) -> tuple[list[str], int, int, bool]:
    """Read lines [offset, offset+limit) of a text file in `cat -n` style.

    Returns (formatted lines, last line shown, total line count, budget_hit). Stops adding
    lines once their size would pass `budget` characters, but still counts the whole file.
    """
    out: list[str] = []
    used = 0
    total = 0
    last = offset - 1
    budget_hit = False
    in_long_line = False
    with path.open("rb") as f:
        for piece in iter(lambda: f.readline(_READ_CHUNK), b""):
            if in_long_line:
                in_long_line = not piece.endswith(b"\n")
                continue
            total += 1
            # A short piece without a newline is the last line of the file, not a cut one.
            in_long_line = len(piece) == _READ_CHUNK and not piece.endswith(b"\n")
            if total < offset or total >= offset + limit or budget_hit:
                continue
            line = piece.decode("utf-8", errors="replace").removesuffix("\n").removesuffix("\r")
            if len(line) > _MAX_LINE_CHARS or in_long_line:
                line = line[:_MAX_LINE_CHARS] + " ... [line truncated]"
            entry = f"{total:>6}\t{line}"
            if out and used + len(entry) + 1 > budget:
                budget_hit = True
                continue
            out.append(entry)
            used += len(entry) + 1
            last = total
    return out, last, total, budget_hit


def _render_text(rel: str, path: Path, offset: int, limit: int, budget: int) -> str:
    """Numbered view of a text file with a header and continuation hints."""
    lines, last, total, budget_hit = _numbered_lines(path, offset, limit, budget)
    if total == 0:
        return f"{rel}: (empty file)"
    if offset > total:
        raise ToolError(f"offset {offset} is past the end of {rel} ({total} lines)")
    parts = [f"{rel}: lines {offset}-{last} of {total}", *lines]
    if budget_hit:
        parts.append(f"(output size limit reached; continue with offset={last + 1})")
    elif last < total:
        parts.append(f"({total - last} more lines; continue with offset={last + 1})")
    return "\n".join(parts)


def _read_text(path: Path, rel: str) -> str:
    """Whole file as UTF-8 with line endings preserved. Raises ToolError for binary files."""
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError as exc:
        raise ToolError(f"{rel} is not UTF-8 text; it cannot be edited as text") from exc


def _write_text(path: Path, content: str) -> None:
    """Write UTF-8 text without newline translation, replacing existing files atomically.

    Existing files keep their permission bits. The temp file lives in the same directory so
    os.replace is a rename on one filesystem.
    """
    if not path.exists():
        with path.open("x", encoding="utf-8", newline="") as f:
            f.write(content)
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _diff(rel: str, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{rel}", f"b/{rel}", n=2
    )
    text = "".join(line if line.endswith("\n") else line + "\n" for line in diff)
    return clip(text.rstrip("\n"), _MAX_DIFF_CHARS) or "(no changes)"


def _replace(content: str, old: str, new: str, replace_all: bool, label: str) -> tuple[str, int]:
    """Apply one exact-match replacement. Returns (new content, replacements made).

    Falls back to CRLF line endings when the file uses them and `old` was written with LF.
    """
    if not old:
        raise ToolError(f"{label}: old_string is empty; use write_file to create or replace a whole file")
    if old == new:
        raise ToolError(f"{label}: old_string and new_string are identical")
    count = content.count(old)
    if count == 0 and "\n" in old and "\r\n" in content and "\r\n" not in old:
        old, new = old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
        count = content.count(old)
    if count == 0:
        raise ToolError(
            f"{label}: old_string not found. Re-read the file and copy the text exactly, "
            "including whitespace and indentation."
        )
    if count > 1 and not replace_all:
        raise ToolError(
            f"{label}: old_string matches {count} places. Add surrounding lines to make it unique, "
            "or set replace_all=true."
        )
    return content.replace(old, new) if replace_all else content.replace(old, new, 1), count if replace_all else 1


def _strip_components(path: str, strip: int) -> str | None:
    if path == "/dev/null":
        return None
    parts = path.split("/")
    if strip and len(parts) <= strip:
        return None
    return "/".join(parts[strip:])


def _unquote(path: str) -> str:
    if path.startswith('"') and path.endswith('"'):
        try:
            return shlex.split(path)[0]
        except ValueError:
            return path.strip('"')
    return path


def patch_paths(patch: str, strip: int) -> list[str]:
    """File paths a unified or git diff touches, after stripping `strip` leading components.

    A `---` line counts as a header only when followed by `+++` and then `@@`, so removed
    lines that start with `--` inside a hunk are not mistaken for headers.
    """
    # Split only on \n like git does; str.splitlines also breaks on \v, \f and others, which
    # would let a header git sees stay hidden from validation.
    lines = [line.removesuffix("\r") for line in patch.split("\n")]
    found: list[str] = []

    def add(raw: str | None) -> None:
        if raw and raw not in found:
            found.append(raw)

    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            rest = line[len("diff --git ") :]
            try:
                names = shlex.split(rest) if rest.startswith('"') else rest.split(" b/", 1)
            except ValueError:
                names = []
            if len(names) != 2:
                continue
            if not rest.startswith('"'):
                names[1] = "b/" + names[1]
            for name in names:
                add(_strip_components(name, strip))
        elif (
            line.startswith("--- ")
            and i + 2 < len(lines)
            and lines[i + 1].startswith("+++ ")
            and lines[i + 2].startswith("@@")
        ):
            for header in (line, lines[i + 1]):
                add(_strip_components(_unquote(header[4:].split("\t")[0].strip()), strip))
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            add(_unquote(line.split(" ", 2)[2].strip()))
    return found


T = TypeVar("T")


def _register(mcp: MCPServer, rt: Runtime) -> None:
    ws = rt.workspace
    limits = rt.config.limits
    worktrees = ws.root / ".dev-link" / "worktrees"
    # Edits read, change and rewrite a file; without this, parallel calls on one file lose updates.
    write_lock = threading.Lock()

    async def in_thread(fn: Callable[..., T], *args: Any) -> T:
        """Run blocking file work off the event loop.

        OS errors become ToolErrors with the reason and path; the SDK would otherwise hide
        everything but the tool name from the agent.
        """
        try:
            return await asyncio.to_thread(fn, *args)
        except OSError as exc:
            where = f": {ws.rel(Path(exc.filename))}" if exc.filename else ""
            raise ToolError(f"{exc.strerror or exc}{where}") from exc

    def entry_path(path: str) -> Path:
        """Resolve the parent through Workspace but keep the last component literal.

        Delete and move must act on a symlink itself, not on what it points to, and must be
        able to remove a link whose target is outside the workspace.
        """
        raw = str(path).strip() or "."
        if "\0" in raw:
            raise ToolError("path contains a NUL byte")
        literal = Path(raw)
        if raw.startswith("~"):
            with contextlib.suppress(RuntimeError):
                literal = literal.expanduser()
        candidate = literal if literal.is_absolute() else ws.root / literal
        # A symlink that points at the root is an ordinary entry; only the root itself is special.
        if literal.name in ("", ".", "..") or (not candidate.is_symlink() and candidate.resolve() == ws.root):
            return ws.resolve(raw)
        parent = ws.resolve(str(literal.parent))
        return parent / literal.name

    def require_file(path: str) -> tuple[Path, str]:
        target = ws.resolve(path, must_exist=True)
        rel = ws.rel(target)
        if target.is_dir():
            raise ToolError(f"{rel} is a directory; use list_dir or tree")
        if not target.is_file():
            raise ToolError(f"{rel} is not a regular file")
        return target, rel

    def read_one(path: str, offset: int, limit: int, budget: int) -> str:
        target, rel = require_file(path)
        if _is_binary(_sniff(target)):
            raise ToolError(f"{rel} is a binary file ({_human(target.stat().st_size)}); use file_info to inspect it")
        return _render_text(rel, target, offset, limit, budget)

    @mcp.tool(annotations=READ_ONLY)
    async def read_file(
        path: Annotated[str, Field(description="Relative to the workspace root.")],
        offset: Annotated[int, Field(ge=1, description="First line to return (1-based).")] = 1,
        limit: Annotated[int, Field(ge=1, le=100_000, description="Maximum number of lines to return.")] = 2000,
    ) -> str | types.ImageContent:
        """Read a text file as numbered lines (`cat -n` style) with the total line count. Output
        stops at the output cap with the offset to continue from; lines over 2000 characters are
        cut. PNG, JPEG, GIF and WebP files up to the read limit are returned as images. Other
        binary files are refused."""
        target, rel = await in_thread(require_file, path)
        mime = _IMAGE_TYPES.get(target.suffix.lower())
        if mime:
            size = target.stat().st_size
            if size > limits.max_read_bytes:
                raise ToolError(f"{rel} is {_human(size)}, over the {_human(limits.max_read_bytes)} read limit")
            data = await in_thread(target.read_bytes)
            return types.ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=mime)
        return await in_thread(read_one, path, offset, limit, limits.max_output_bytes)

    @mcp.tool(annotations=READ_ONLY)
    async def read_many_files(
        paths: Annotated[list[str], Field(min_length=1, max_length=100)],
        limit_per_file: Annotated[int, Field(ge=1, le=100_000, description="Maximum lines per file.")] = 500,
    ) -> str:
        """Read several text files in one call, each as numbered lines under a `==> path <==` header.
        A file that cannot be read gets an inline error and the rest are still returned. Files past
        the output cap and images are skipped; use read_file for them."""

        def run() -> str:
            budget = limits.max_output_bytes
            sections: list[str] = []
            for path in paths:
                if budget <= 0:
                    sections.append(f"==> {path} <==\n(skipped: output size limit reached; read it separately)")
                    continue
                try:
                    target, _ = require_file(path)
                    if target.suffix.lower() in _IMAGE_TYPES:
                        body = "(image file; use read_file to view it)"
                    else:
                        body = read_one(path, 1, limit_per_file, budget)
                except ToolError as exc:
                    body = f"error: {exc}"
                except OSError as exc:
                    body = f"error: {exc.strerror or exc}"
                budget -= len(body)
                sections.append(f"==> {path} <==\n{body}")
            return "\n\n".join(sections)

        return await in_thread(run)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def write_file(
        path: Annotated[str, Field(description="Relative to the workspace root.")],
        content: Annotated[str, Field(description="Full new file content (UTF-8). Line endings are kept as given.")],
        create_dirs: Annotated[bool, Field(description="Create missing parent directories.")] = True,
    ) -> str:
        """Create a file or replace its whole content; use edit_file or multi_edit to change part of
        a file. An existing file is replaced atomically and keeps its permissions."""

        def run() -> str:
            target = ws.resolve(path)
            rel = ws.rel(target)
            if target.is_dir():
                raise ToolError(f"{rel} is a directory")
            if not target.parent.is_dir():
                if not create_dirs:
                    raise ToolError(f"parent directory of {rel} does not exist; set create_dirs=true")
                target.parent.mkdir(parents=True, exist_ok=True)
            with write_lock:
                existed = target.exists()
                _write_text(target, content)
            size = len(content.encode("utf-8"))
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            return f"{'overwrote' if existed else 'created'} {rel} ({size} bytes, {line_count} lines)"

        return await in_thread(run)

    def edit(path: str, edits: list[EditSpec]) -> str:
        target, rel = require_file(path)
        with write_lock:
            before = _read_text(target, rel)
            after = before
            total = 0
            for index, spec in enumerate(edits, 1):
                label = f"edit {index}" if len(edits) > 1 else "edit"
                after, count = _replace(after, spec.old_string, spec.new_string, spec.replace_all, label)
                total += count
            _write_text(target, after)
        noun = "replacement" if total == 1 else "replacements"
        return f"edited {rel}: {total} {noun}\n{_diff(rel, before, after)}"

    @mcp.tool(annotations=WRITE)
    async def edit_file(
        path: Annotated[str, Field(description="Relative to the workspace root.")],
        old_string: Annotated[str, Field(description="Exact text to replace, including whitespace and indentation.")],
        new_string: Annotated[str, Field(description="Replacement text.")],
        replace_all: Annotated[bool, Field(description="Replace every occurrence.")] = False,
    ) -> str:
        """Replace an exact string in a file and return a unified diff. Fails when the text is not
        found, or matches more than once and replace_all is false."""
        return await in_thread(
            edit, path, [EditSpec(old_string=old_string, new_string=new_string, replace_all=replace_all)]
        )

    @mcp.tool(annotations=WRITE)
    async def multi_edit(
        path: Annotated[str, Field(description="Relative to the workspace root.")],
        edits: Annotated[
            list[EditSpec], Field(min_length=1, description="Edits applied in order; each sees the previous result.")
        ],
    ) -> str:
        """Apply several exact-string edits to one file, all or nothing: if any edit fails, the file
        is left unchanged and the error names the edit. Returns a unified diff of the result."""
        return await in_thread(edit, path, edits)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def apply_patch(
        patch: Annotated[str, Field(description="Unified diff (git diff or diff -u); may touch many files.")],
        dry_run: Annotated[bool, Field(description="Only check that the patch applies; change nothing.")] = False,
        strip: Annotated[
            int, Field(ge=0, le=10, description="Leading path components to drop (1 for a/ and b/ prefixes).")
        ] = 1,
    ) -> str:
        """Apply a unified diff with `git apply`, also outside git repos. Patch paths are relative to
        the workspace root. Can create, delete and rename files; hunk line counts are recomputed, so
        slightly wrong @@ headers still apply. Nothing changes if any hunk fails."""
        paths = patch_paths(patch, strip)
        if not paths:
            raise ToolError("no file headers found in the patch; expected '--- a/path' and '+++ b/path' lines")
        for p in paths:
            ws.resolve(p)
        root = ws.root
        argv = ["git", "apply", "--verbose", "--recount", "--whitespace=nowarn", f"-p{strip}"]
        if dry_run:
            argv.append("--check")
        argv.append("-")
        # Stop git from finding a repository above the workspace when the sandbox is off.
        env = {"GIT_CEILING_DIRECTORIES": str(root.parent)}
        body = patch if patch.endswith("\n") else patch + "\n"
        result = await rt.sandbox.run(argv, cwd=root, stdin=body, timeout_s=60, env=env)
        output = (result.stderr + result.stdout).strip()
        if not result.ok:
            raise ToolError(f"patch does not apply; nothing was changed\n{clip(output, limits.max_output_bytes)}")
        verb = "applies cleanly to" if dry_run else "applied to"
        listing = "\n".join(f"  {p}" for p in paths)
        return f"patch {verb} {len(paths)} path(s):\n{listing}\n{clip(output, limits.max_output_bytes)}".rstrip()

    @mcp.tool(annotations=READ_ONLY)
    async def list_dir(
        path: Annotated[str, Field(description="Relative to the workspace root.")] = ".",
        show_hidden: Annotated[bool, Field(description="Include entries whose names start with a dot.")] = False,
    ) -> str:
        """List one directory: directories first, then files with sizes, and symlinks with their
        targets. Use tree for a recursive view."""

        def run() -> str:
            target = ws.resolve(path, must_exist=True)
            rel = ws.rel(target)
            if not target.is_dir():
                raise ToolError(f"{rel} is not a directory; use file_info or read_file")
            entries = sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower()))
            hidden = 0
            rows: list[str] = []
            for entry in entries:
                if entry.name.startswith(".") and not show_hidden:
                    hidden += 1
                    continue
                if entry.is_symlink():
                    rows.append(f"link {'':>8} {entry.name} -> {os.readlink(entry.path)}")
                elif entry.is_dir():
                    rows.append(f"dir  {'':>8} {entry.name}/")
                else:
                    try:
                        size = _human(entry.stat().st_size)
                    except OSError:
                        size = "?"
                    rows.append(f"file {size:>8} {entry.name}")
            summary = f"{rel}: {len(rows)} entries"
            if hidden:
                summary += f" ({hidden} hidden; set show_hidden=true to see them)"
            return clip("\n".join([summary, *rows]), limits.max_output_bytes)

        return await in_thread(run)

    @mcp.tool(annotations=READ_ONLY)
    async def tree(
        path: Annotated[str, Field(description="Relative to the workspace root.")] = ".",
        max_depth: Annotated[int, Field(ge=1, le=20, description="How many directory levels to descend.")] = 3,
        max_entries: Annotated[int, Field(ge=1, le=10_000, description="Stop after this many entries.")] = 500,
    ) -> str:
        """Recursive directory listing. Generated and vendored directories (.git, node_modules, .venv,
        __pycache__, dist, build, target, caches) are listed as skipped, symlinks are not followed,
        and directories at max_depth end with '/ ...'."""

        def run() -> str:
            target = ws.resolve(path, must_exist=True)
            if not target.is_dir():
                raise ToolError(f"{ws.rel(target)} is not a directory")
            lines = [f"{ws.rel(target)}/"]
            count = 0
            truncated = False

            def walk(directory: Path, depth: int) -> None:
                nonlocal count, truncated
                try:
                    entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(), e.name.lower()))
                except OSError as exc:
                    lines.append(f"{'  ' * depth}(unreadable: {exc.strerror})")
                    return
                for entry in entries:
                    if count >= max_entries:
                        truncated = True
                        return
                    count += 1
                    indent = "  " * depth
                    if entry.is_symlink():
                        lines.append(f"{indent}{entry.name} -> {os.readlink(entry.path)}")
                    elif entry.is_dir():
                        if entry.name in _TREE_SKIP or Path(entry.path) == worktrees:
                            lines.append(f"{indent}{entry.name}/ (skipped)")
                        elif depth >= max_depth:
                            lines.append(f"{indent}{entry.name}/ ...")
                        else:
                            lines.append(f"{indent}{entry.name}/")
                            walk(Path(entry.path), depth + 1)
                            if truncated:
                                return
                    else:
                        lines.append(f"{indent}{entry.name}")

            walk(target, 1)
            if truncated:
                lines.append(f"(stopped at {max_entries} entries; narrow the path or raise max_entries)")
            return clip("\n".join(lines), limits.max_output_bytes)

        return await in_thread(run)

    @mcp.tool(annotations=READ_ONLY)
    async def file_info(
        path: Annotated[str, Field(description="Relative to the workspace root.")],
    ) -> dict[str, Any]:
        """Metadata for a path: type, size, modification time, permissions, MIME guess, whether it is
        binary, line count for text files up to the read limit, entry count for directories, and
        symlink target."""

        def run() -> dict[str, Any]:
            link = entry_path(path)
            link_target = os.readlink(link) if link.is_symlink() else None
            target = ws.resolve(path, must_exist=True)
            st = target.stat()
            info: dict[str, Any] = {
                "path": ws.rel(target),
                "type": "directory" if target.is_dir() else "file" if target.is_file() else "other",
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
                "mode": oct(stat.S_IMODE(st.st_mode)),
            }
            if link_target is not None:
                info["symlink"] = {"path": ws.rel(link), "target": link_target}
            if target.is_dir():
                info["entries"] = sum(1 for _ in os.scandir(target))
            elif target.is_file():
                info["mime_type"] = mimetypes.guess_type(target.name)[0]
                info["binary"] = _is_binary(_sniff(target))
                if not info["binary"] and st.st_size <= limits.max_read_bytes:
                    _, _, info["lines"], _ = _numbered_lines(target, 1, 1, 0)
            return info

        return await in_thread(run)

    @mcp.tool(annotations=WRITE)
    async def make_dir(
        path: Annotated[str, Field(description="Directory to create, relative to the workspace root.")],
    ) -> str:
        """Create a directory and any missing parents. Succeeds if it already exists."""

        def run() -> str:
            target = ws.resolve(path)
            rel = ws.rel(target)
            if target.exists() and not target.is_dir():
                raise ToolError(f"{rel} exists and is not a directory")
            if target.is_dir():
                return f"{rel} already exists"
            target.mkdir(parents=True)
            return f"created {rel}/"

        return await in_thread(run)

    def prepare_destination(src: Path, dst: Path, overwrite: bool) -> None:
        """Check a move or copy destination and clear it when overwriting is allowed."""
        rel = ws.rel(dst)
        if dst == ws.root:
            raise ToolError("destination cannot be the workspace root")
        if src.is_dir() and not src.is_symlink() and dst.is_relative_to(src):
            raise ToolError(f"cannot put {ws.rel(src)} inside itself ({rel})")
        if src.is_relative_to(dst):
            raise ToolError(f"{rel} contains the source {ws.rel(src)}; replacing it would destroy the source")
        if dst.exists() or dst.is_symlink():
            if not overwrite:
                raise ToolError(f"{rel} already exists; set overwrite=true to replace it")
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def move_path(
        src: Annotated[str, Field(description="File, directory or symlink to move.")],
        dst: Annotated[str, Field(description="Full destination path (not a directory to move into).")],
        overwrite: Annotated[bool, Field(description="Replace an existing destination.")] = False,
    ) -> str:
        """Move or rename a file, directory or symlink. `dst` is the new path itself. Missing parent
        directories are created. Fails if `dst` exists unless overwrite=true. Symlinks are moved as
        links."""

        def run() -> str:
            source = entry_path(src)
            if source == ws.root:
                raise ToolError("cannot move the workspace root")
            if not source.exists() and not source.is_symlink():
                raise ToolError(f"path does not exist: {ws.rel(source)}")
            target = entry_path(dst)
            if target == source:
                raise ToolError("source and destination are the same path")
            prepare_destination(source, target, overwrite)
            shutil.move(source, target)
            return f"moved {ws.rel(source)} -> {ws.rel(target)}"

        return await in_thread(run)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def copy_path(
        src: Annotated[str, Field(description="File or directory to copy.")],
        dst: Annotated[str, Field(description="Full destination path (not a directory to copy into).")],
        overwrite: Annotated[bool, Field(description="Replace an existing destination.")] = False,
    ) -> str:
        """Copy a file or a whole directory tree, keeping timestamps and permissions. `dst` is the new
        path itself. Symlinks inside a copied directory are copied as links. Fails if `dst` exists
        unless overwrite=true."""

        def run() -> str:
            source = ws.resolve(src, must_exist=True)
            target = entry_path(dst)
            if target == source:
                raise ToolError("source and destination are the same path")
            prepare_destination(source, target, overwrite)
            if source.is_dir():
                # symlinks=True keeps links as links; following them could copy files from outside the workspace.
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
            return f"copied {ws.rel(source)} -> {ws.rel(target)}"

        return await in_thread(run)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_path(
        path: Annotated[str, Field(description="File, directory or symlink to delete.")],
        recursive: Annotated[bool, Field(description="Required to delete a non-empty directory.")] = False,
    ) -> str:
        """Delete a file, symlink (the link only, never its target) or directory. A non-empty directory
        needs recursive=true. The workspace root cannot be deleted. There is no undo outside
        checkpoints or git."""

        def run() -> str:
            target = entry_path(path)
            if target == ws.root:
                raise ToolError("refusing to delete the workspace root")
            rel = ws.rel(target)
            if target.is_symlink() or target.is_file():
                target.unlink()
                return f"deleted {rel}"
            if not target.exists():
                raise ToolError(f"path does not exist: {rel}")
            if target.is_dir():
                if recursive:
                    shutil.rmtree(target)
                    return f"deleted directory {rel}/ and its contents"
                try:
                    target.rmdir()
                except OSError as exc:
                    raise ToolError(f"{rel}/ is not empty; set recursive=true to delete it and its contents") from exc
                return f"deleted empty directory {rel}/"
            target.unlink()
            return f"deleted {rel}"

        return await in_thread(run)


class FilesGroup(ToolGroup):
    """Reading, writing, editing and organising files inside the workspace."""

    name = "files"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)
