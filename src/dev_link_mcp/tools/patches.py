"""Patch formats that coding models write, parsed into file changes and applied to text.

`apply_patch` hands unified diffs with numbered hunks to `git apply`. Everything else lands here:

- OpenAI's `*** Begin Patch` format (Codex, Cline, GPT models).
- SEARCH/REPLACE blocks (aider, Cline, Roo Code), with the file path on a line before each block.
- Unified diffs whose hunk headers have no line numbers (`@@ ... @@`, aider's udiff).

Each is turned into `FileChange`s; `apply_hunks` then finds the old lines in a file's text,
tolerating trailing-whitespace, indentation and typographic-punctuation drift the way Codex does.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal

from mcp.server.mcpserver.exceptions import ToolError

PatchFormat = Literal["openai", "search_replace", "unified"]

# Lines that wrap a patch as a heredoc, which models copy from shell-tool examples.
_HEREDOC_STARTS = ("<<EOF", "<<'EOF'", '<<"EOF"')
# An opening fence with at most a language tag; ```src/app.py names a file and must stay.
_FENCE_OPEN = re.compile(r"^```[\w+-]*$")
_NUMBERED_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")

# Markers count only at the start of a line, so diff context lines (" <<<<<<< SEARCH") never do.
# aider writes 5-9 marker characters, Cline 3 or more; Cline also uses - and + instead of < and >.
_SEARCH = re.compile(r"^(?:<{3,}|-{3,}) ?SEARCH>?\s*$")
_DIVIDER = re.compile(r"^={3,}\s*$")
_REPLACE = re.compile(r"^(?:>{3,}|\+{3,}) ?REPLACE>?\s*$")
# Roo Code puts line hints and a `-------` separator at the top of the SEARCH part.
_LINE_HINT = re.compile(r"^:(?:start|end)_line:\s*\d*\s*$")
_ROO_SEPARATOR = re.compile(r"^-{3,}\s*$")
# Roo Code escapes content lines that look like markers with a leading backslash.
_ESCAPED_MARKER = re.compile(r"^\\(?=<{3,}|={3,}|>{3,}|-{3,}|\+{3,}|:start_line:|:end_line:)")

# git header lines that may sit between file sections of a unified diff.
_GIT_HEADERS = ("diff --git ", "index ", "new file mode ", "deleted file mode ", "similarity index ")
# git sections that only `git apply` can carry out; they need no text hunks to match.
_GIT_ONLY = ("rename from ", "rename to ", "copy from ", "copy to ", "old mode ", "new mode ", "Binary files ")

_DASHES = str.maketrans(dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"))
_QUOTES = str.maketrans(
    {**dict.fromkeys("\u2018\u2019\u201a\u201b", "'"), **dict.fromkeys("\u201c\u201d\u201e\u201f", '"')}
)
_SPACES = str.maketrans(
    dict.fromkeys("\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000", " ")
)


@dataclass
class Hunk:
    """One replacement inside a file.

    `old` lines are replaced by `new` lines. `anchors` are lines that must be found, in order,
    before `old` (OpenAI `@@ class Foo` lines). `at_end` pins `old` to the end of the file.
    `unique` means `old` is searched in the whole file and must match exactly one place, which is
    how SEARCH/REPLACE blocks behave; otherwise hunks are searched in order after the previous one,
    and `line_hint` (0-based, from a numbered @@ header) picks the nearest of several matches.
    An empty `old` appends `new` to the end of the file.
    """

    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    at_end: bool = False
    unique: bool = False
    line_hint: int | None = None
    # Trailing lines that were completely empty in the patch; usually spacing, not context.
    blank_tail: int = 0

    def add(self, kind: str, text: str) -> None:
        """Record one patch line: `kind` is ' ' or '' for context, '-' removed, '+' added."""
        self.blank_tail = self.blank_tail + 1 if kind == "" else 0
        if kind != "+":
            self.old.append(text)
        if kind != "-":
            self.new.append(text)

    def drop_blank_tail(self) -> None:
        if self.blank_tail:
            del self.old[-self.blank_tail :]
            del self.new[-self.blank_tail :]
            self.blank_tail = 0


@dataclass
class FileChange:
    """What a patch does to one path, relative to the workspace root as written in the patch.

    `add` writes `content` (replacing any existing file), `delete` removes the file and `update`
    applies `hunks`, then renames the file to `move_to` when set.
    """

    action: Literal["add", "delete", "update"]
    path: str
    content: str = ""
    hunks: list[Hunk] = field(default_factory=list)
    move_to: str | None = None


def unwrap(patch: str) -> str:
    """Drop a markdown fence or heredoc that wraps the whole patch, and surrounding blank lines."""
    lines = patch.strip("\n").split("\n")
    if len(lines) >= 2 and _FENCE_OPEN.match(lines[0].strip()) and lines[-1].strip() == "```":
        lines = lines[1:-1]
    if len(lines) >= 2 and lines[0].strip() in _HEREDOC_STARTS and lines[-1].strip().endswith("EOF"):
        lines = lines[1:-1]
    return "\n".join(lines) + "\n"


def detect_format(patch: str) -> PatchFormat | None:
    """The format of the first marker in the patch, or None when there is none.

    The first marker decides, so a diff of a file that contains SEARCH markers stays a diff,
    and a SEARCH block whose text contains `*** Begin Patch` stays a SEARCH block.
    """
    lines = _lines(patch)
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if line.startswith("diff --git ") or _is_file_header(lines, i):
            return "unified"
        if line == "*** Begin Patch":
            return "openai"
        if _SEARCH.match(line):
            return "search_replace"
    return None


def has_unnumbered_hunks(patch: str) -> bool:
    """True when some `@@` hunk header has no line numbers, which `git apply` cannot place."""
    return any(line.startswith("@@") and not _NUMBERED_HUNK.match(line) for line in _lines(patch))


def default_strip(patch: str) -> int:
    """Path components to drop from unified-diff paths: 1 for git's a/ and b/ prefixes, else 0."""
    lines = _lines(patch)
    old: list[str] = []
    new: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            return 1
        if _is_file_header(lines, i):
            for name, side in ((_header_name(line), old), (_header_name(lines[i + 1]), new)):
                if name != "/dev/null":
                    side.append(name)
    if old or new:
        return int(all(n.startswith("a/") for n in old) and all(n.startswith("b/") for n in new))
    return 1


def parse_openai_patch(patch: str) -> list[FileChange]:
    """Parse OpenAI's `*** Begin Patch` ... `*** End Patch` format.

    Text before `*** Begin Patch` is ignored. Markers must start the line; a context line that
    happens to read ` *** End Patch` is content. A missing `*** End Patch` is an error, since it
    usually means the patch was cut off. Raises ToolError naming the offending line.
    """
    lines = _lines(patch)
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "*** Begin Patch")
    changes: list[FileChange] = []
    current: FileChange | None = None
    hunk: Hunk | None = None
    # Anchors read since the last hunk line; several in a row narrow down nested scopes.
    pending: list[str] = []

    def fail(number: int, message: str) -> ToolError:
        return ToolError(f"line {number}: {message}")

    def close_hunk() -> None:
        if hunk is not None and current is not None:
            hunk.drop_blank_tail()
            if not hunk.old and not hunk.new:
                current.hunks.remove(hunk)

    def finish() -> None:
        close_hunk()
        if current is not None and current.action == "update":
            if not current.hunks and current.move_to is None:
                raise ToolError(f"update for {current.path} has no changes")
            if pending:
                raise ToolError(f"update for {current.path} ends with '@@' but no changed lines")

    for number, raw in enumerate(lines[start + 1 :], start + 2):
        marker = raw.rstrip()
        if marker == "*** End Patch":
            finish()
            if not changes:
                raise ToolError("the patch has no file sections")
            return changes
        if marker.startswith(("*** Add File:", "*** Delete File:", "*** Update File:")):
            finish()
            kind, _, path = marker[4:].partition(":")
            path = path.strip()
            if not path:
                raise fail(number, f"'{marker}' has no path")
            action: Literal["add", "delete", "update"] = (
                "add" if kind == "Add File" else "delete" if kind == "Delete File" else "update"
            )
            current = FileChange(action, path)
            changes.append(current)
            hunk, pending = None, []
            continue
        if current is None:
            if not marker:
                continue
            raise fail(number, f"expected '*** Add File:', '*** Delete File:' or '*** Update File:', got '{marker}'")
        if current.action == "add":
            if not raw.startswith("+"):
                raise fail(number, f"every line of an added file must start with '+', got '{raw}'")
            current.content += raw[1:] + "\n"
            continue
        if current.action == "delete":
            if marker:
                raise fail(number, f"a deleted file takes no lines, got '{raw}'")
            continue
        if marker.startswith("*** Move to:") and not current.hunks and hunk is None:
            current.move_to = marker[len("*** Move to:") :].strip()
            if not current.move_to:
                raise fail(number, "'*** Move to:' has no path")
            continue
        if marker == "*** End of File":
            if hunk is None:
                raise fail(number, "'*** End of File' before any changed lines")
            hunk.at_end = True
            close_hunk()
            hunk = None
            continue
        if marker == "@@" or raw.startswith("@@ "):
            close_hunk()
            anchor = raw[2:].strip()
            if anchor:
                pending.append(anchor)
            hunk = None
            continue
        if raw[:1] not in (" ", "+", "-", ""):
            raise fail(
                number,
                f"'{raw}' is not a change line; each line starts with ' ' (context), '+' (added) or '-' (removed)",
            )
        if hunk is None:
            hunk = Hunk(anchors=pending)
            pending = []
            current.hunks.append(hunk)
        hunk.add(raw[:1], raw[1:])
    raise ToolError("the patch has no '*** End Patch' line; it may have been cut off")


def parse_search_replace(patch: str) -> list[FileChange]:
    """Parse SEARCH/REPLACE blocks, each preceded by its file path.

    The path is found on one of the three lines before `<<<<<<< SEARCH`, skipping fence lines
    (aider's `diff` and `diff-fenced` layouts). A block with no path reuses the previous block's.
    Blocks for one file are applied in order. Raises ToolError for unbalanced markers.
    """
    lines = _lines(patch)
    by_path: dict[str, FileChange] = {}
    path: str | None = None
    i = 0
    while i < len(lines):
        if not _SEARCH.match(lines[i].rstrip()):
            i += 1
            continue
        path = _block_path(lines[max(0, i - 3) : i]) or path
        if path is None:
            raise ToolError(
                f"line {i + 1}: no file path before the SEARCH block; put the path alone on the line before it"
            )
        start = i + 1
        i = start
        while i < len(lines) and not _DIVIDER.match(lines[i].rstrip()):
            i += 1
        if i == len(lines):
            raise ToolError(f"line {start}: SEARCH block for {path} has no '=======' line")
        search = lines[start:i]
        middle = i + 1
        i = middle
        while i < len(lines) and not _REPLACE.match(lines[i].rstrip()):
            i += 1
        if i == len(lines):
            raise ToolError(f"line {start}: SEARCH block for {path} has no '>>>>>>> REPLACE' line")
        replace = lines[middle:i]
        i += 1
        while search and _LINE_HINT.match(search[0].strip()):
            search = search[1:]
            if search and _ROO_SEPARATOR.match(search[0].strip()):
                search = search[1:]
        search = [_ESCAPED_MARKER.sub("", line) for line in search]
        replace = [_ESCAPED_MARKER.sub("", line) for line in replace]
        if not any(line.strip() for line in search):
            search = []
        change = by_path.setdefault(path, FileChange("update", path))
        change.hunks.append(Hunk(old=search, new=replace, unique=True))
    return list(by_path.values())


def parse_unnumbered_diff(patch: str, strip: int) -> list[FileChange]:
    """Parse a unified diff in which some hunks lack line numbers; hunks are located by their text.

    Line numbers that are present only break ties between several matches. `--- /dev/null` adds
    a file, `+++ /dev/null` deletes one, and different old and new paths rename it. Raises
    ToolError for a line that is not part of a hunk, and for renames, copies, mode changes and
    binary files without hunks, which only `git apply` can do.
    """
    lines = _lines(patch)
    changes: list[FileChange] = []
    current: FileChange | None = None
    hunk: Hunk | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(_GIT_ONLY) or line.rstrip() == "GIT binary patch":
            raise ToolError(
                f"line {i + 1}: '{line}' needs git; send this diff with numbered hunks "
                "(@@ -start,count +start,count @@)"
            )
        if _is_file_header(lines, i):
            if hunk is not None:
                hunk.drop_blank_tail()
            old = _diff_path(line, strip)
            new = _diff_path(lines[i + 1], strip)
            if old is None and new is None:
                raise ToolError(f"line {i + 1}: both sides of the file header are /dev/null")
            if old is None:
                current = FileChange("add", new or "")
            elif new is None:
                current = FileChange("delete", old)
            else:
                current = FileChange("update", old, move_to=new if new != old else None)
            changes.append(current)
            hunk = None
            i += 2
            continue
        if line.startswith("@@"):
            if current is None:
                raise ToolError(f"line {i + 1}: hunk before any '--- a/path' / '+++ b/path' header")
            if hunk is not None:
                hunk.drop_blank_tail()
            numbered = _NUMBERED_HUNK.match(line)
            hunk = Hunk(line_hint=max(int(numbered.group(1)) - 1, 0) if numbered else None)
            current.hunks.append(hunk)
        elif line.startswith(_GIT_HEADERS) or line.startswith("\\"):
            if hunk is not None and not line.startswith("\\"):
                hunk.drop_blank_tail()
                hunk = None
        elif hunk is not None:
            if line[:1] not in (" ", "+", "-", ""):
                raise ToolError(
                    f"line {i + 1}: '{line}' is not a diff line; "
                    "each line starts with ' ' (context), '+' (added) or '-' (removed)"
                )
            hunk.add(line[:1], line[1:])
        i += 1
    if hunk is not None:
        hunk.drop_blank_tail()
    for change in changes:
        if change.action == "add":
            change.content = "".join(line + "\n" for h in change.hunks for line in h.new)
            change.hunks = []
    return changes


def apply_hunks(content: str, hunks: list[Hunk], label: str) -> str:
    """Return `content` with `hunks` applied in order; `label` names the file in errors.

    Each line keeps its own ending (LF or CRLF); added lines use CRLF when the file has any,
    and the presence of a final newline is kept. Lines that a hunk keeps as context keep the
    file's own text even when they were matched loosely. Raises ToolError when a hunk's lines
    or anchors cannot be found, or a unique hunk is ambiguous.
    """
    default = "\r\n" if "\r\n" in content else "\n"
    parts = content.split("\n")
    texts = [p.removesuffix("\r") for p in parts[:-1]] + [parts[-1]]
    # None marks an added line, which takes the default ending.
    ends: list[str | None] = ["\r\n" if p.endswith("\r") else "\n" for p in parts[:-1]]
    ends.append("")
    final_newline = texts[-1] == ""
    if final_newline:
        texts.pop()
        ends.pop()
    cursor = 0
    for number, hunk in enumerate(hunks, 1):
        where = f"{label}: hunk {number}" if len(hunks) > 1 else label
        for anchor in hunk.anchors:
            found = _seek(texts, [anchor], cursor, at_end=False)
            if found is None:
                raise ToolError(f"{where}: context line not found: {anchor!r}")
            cursor = found + 1
        old, new = hunk.old, hunk.new
        if not old:
            if hunk.unique and texts:
                # aider appends here and Cline replaces the whole file; guessing could lose data.
                raise ToolError(
                    f"{where}: the SEARCH part is empty but the file already has content; quote the lines "
                    "to replace, or use write_file to replace the whole file"
                )
            texts.extend(new)
            ends.extend([None] * len(new))
            cursor = len(texts)
            continue
        start = 0 if hunk.unique else cursor
        found = _seek(texts, old, start, hunk.at_end, unique=hunk.unique, hint=hunk.line_hint, where=where)
        if found is None and old[-1] == "" and len(old) > 1:
            old = old[:-1]
            new = new[:-1] if new and new[-1] == "" else new
            found = _seek(texts, old, start, hunk.at_end, unique=hunk.unique, hint=hunk.line_hint, where=where)
        if found is None:
            shown = "\n".join(hunk.old[:20]) + ("\n..." if len(hunk.old) > 20 else "")
            raise ToolError(f"{where}: these lines were not found; re-read the file and copy them exactly:\n{shown}")
        span = slice(found, found + len(old))
        new_texts, new_ends = _keep_file_context(texts[span], ends[span], old, new)
        texts[span] = new_texts
        ends[span] = new_ends
        cursor = found + len(new_texts)
    if not texts:
        return ""
    last = len(texts) - 1
    out = []
    for i, (text, end) in enumerate(zip(texts, ends, strict=True)):
        if i == last:
            end = (default if end in (None, "") else end) if final_newline else ""
        elif end in (None, ""):
            end = default
        out.append(text + end)
    return "".join(out)


def _keep_file_context(
    matched: list[str], matched_ends: list[str | None], old: list[str], new: list[str]
) -> tuple[list[str], list[str | None]]:
    """`new`, but with lines it shares with `old` taken (with their endings) from the file."""
    texts: list[str] = []
    ends: list[str | None] = []
    for tag, o1, o2, n1, n2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag == "equal":
            texts.extend(matched[o1:o2])
            ends.extend(matched_ends[o1:o2])
        else:
            texts.extend(new[n1:n2])
            ends.extend([None] * (n2 - n1))
    return texts, ends


def _seek(
    lines: list[str],
    pattern: list[str],
    start: int,
    at_end: bool,
    *,
    unique: bool = False,
    hint: int | None = None,
    where: str = "",
) -> int | None:
    """Index where `pattern` starts in `lines` at or after `start`, or None.

    Tries an exact match first, then ignores trailing whitespace, then all surrounding
    whitespace, then also typographic dashes, quotes and spaces. With `unique`, the first level
    that matches must match exactly once, else ToolError. Otherwise the first match wins, or the
    one nearest `hint` when given.
    """
    if len(pattern) > len(lines):
        return None
    last = len(lines) - len(pattern)
    if at_end:
        start = max(start, last)
    for norm in (_same, str.rstrip, str.strip, _plain):
        wanted = [norm(p) for p in pattern]
        hits = [i for i in range(start, last + 1) if all(norm(lines[i + k]) == w for k, w in enumerate(wanted))]
        if hits:
            if unique and len(hits) > 1:
                raise ToolError(
                    f"{where}: the SEARCH text matches {len(hits)} places; add surrounding lines to make it unique"
                )
            if hint is not None:
                return min(hits, key=lambda i: abs(i - hint))
            return hits[0]
    return None


def _same(line: str) -> str:
    return line


def _plain(line: str) -> str:
    return line.strip().translate(_DASHES).translate(_QUOTES).translate(_SPACES)


def _lines(patch: str) -> list[str]:
    # Split only on \n like git does, so validation and application see the same lines.
    return [line.removesuffix("\r") for line in patch.split("\n")]


def _is_file_header(lines: list[str], i: int) -> bool:
    """Same rule as patch_paths: `---` counts only when followed by `+++` and then `@@`, so a
    removed line that starts with `-- ` is not a header."""
    return (
        lines[i].startswith("--- ")
        and i + 2 < len(lines)
        and lines[i + 1].startswith("+++ ")
        and lines[i + 2].startswith("@@")
    )


def _header_name(header: str) -> str:
    name = header[4:].split("\t")[0].strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name


def _diff_path(header: str, strip: int) -> str | None:
    name = _header_name(header)
    if name == "/dev/null":
        return None
    parts = name.split("/")
    if strip and len(parts) <= strip:
        raise ToolError(f"cannot drop {strip} path component(s) from '{name}'; set strip=0")
    return "/".join(parts[strip:])


def _block_path(before: list[str]) -> str | None:
    """File path on the lines before a SEARCH marker, nearest first, looking past fence lines.

    A path is a single word: a line with spaces is prose, and so is a word ending in ':' that
    has no '.' or '/' ("Then:"). Paths that contain spaces are not supported here.
    """
    for line in reversed(before):
        text = line.strip()
        if text.startswith("```"):
            candidate = text[3:].strip()
            # ```python is a language tag; ```src/app.py is a path.
            if "." in candidate or "/" in candidate:
                return candidate
            continue
        if _REPLACE.match(text) or _DIVIDER.match(text):
            return None
        colon = text.endswith(":")
        text = text.rstrip(":").lstrip("#").strip().strip("`*").strip()
        if not text or any(c.isspace() for c in text):
            return None
        if colon and "." not in text and "/" not in text:
            return None
        return text
    return None
