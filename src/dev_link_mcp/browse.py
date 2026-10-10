"""Read-only view of the workspace for the dashboard's Files tab.

Paths go through a confined Workspace even when the server runs without the sandbox: the
dashboard only ever shows the workspace. `.git` internals and the server's state dir are
never listed or served.
"""

from __future__ import annotations

import codecs
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from mcp.server.mcpserver.exceptions import ToolError

from dev_link_mcp.workspace import Workspace, read_at_most

LIST_MAX = 2000
TEXT_DEFAULT = 256 * 1024
RAW_MAX = 1024 * 1024
IMAGE_MAX = 16 * 1024 * 1024
_SNIFF = 8192

Kind = Literal["text", "image", "binary"]

# Raster formats a browser shows without running anything. SVG is left out on purpose: it can
# carry script, so it is served as text.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class BrowseError(Exception):
    """A request the Files API refuses; `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Chunk:
    """Bytes of one file for a preview.

    `kind` is how the content was sniffed. `mime` is the Content-Type to send. `offset` and
    `next` are byte offsets of this chunk in the file; `size` is the file size in bytes.
    """

    kind: Kind
    mime: str
    data: bytes
    offset: int
    next: int
    size: int


def _image_mime(head: bytes) -> str | None:
    for magic, mime in _IMAGE_MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


def sniff(head: bytes, whole: bool) -> tuple[Kind, str]:
    """(kind, Content-Type) for a file from its first bytes, ignoring its name.

    `whole` says `head` is the entire file, so a cut-off character at its end is invalid.
    NUL bytes or invalid UTF-8 mean binary. HTML, SVG and every other text format are text,
    so a preview never renders active content.
    """
    mime = _image_mime(head)
    if mime is not None:
        return "image", mime
    if b"\0" in head:
        return "binary", "application/octet-stream"
    try:
        # Unless it is the whole file, the head may end in the middle of a multi-byte character.
        codecs.getincrementaldecoder("utf-8")().decode(head, final=whole)
    except UnicodeDecodeError:
        return "binary", "application/octet-stream"
    return "text", "text/plain; charset=utf-8"


def _utf8_bounds(data: bytes, at_start: bool, at_end: bool) -> tuple[int, int]:
    """Start and end indexes inside `data` that do not cut a UTF-8 character in two."""
    start = 0
    if not at_start:
        # Continuation bytes (10xxxxxx) at the front belong to a character that began earlier.
        while start < min(len(data), 3) and data[start] & 0xC0 == 0x80:
            start += 1
    end = len(data)
    if not at_end:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        decoder.decode(data[start:], final=False)
        end -= len(decoder.getstate()[0])
    return start, end


class FileBrowser:
    """Lists directories and reads file chunks inside `root`, never outside it."""

    def __init__(self, root: Path, state_dir: Path | None):
        self.workspace = Workspace(root, confined=True)
        self.state_dir = state_dir.resolve() if state_dir is not None else None

    def _blocked(self, path: Path) -> str | None:
        rel = path.relative_to(self.workspace.root)
        if ".git" in rel.parts:
            return ".git internals are not served"
        if self.state_dir is not None and path.is_relative_to(self.state_dir):
            return "the server state directory is not served"
        return None

    def resolve(self, raw: str) -> Path:
        """Map a dashboard path (workspace-relative or absolute) to an existing host path.

        Raises BrowseError 403 for paths outside the workspace (including through symlinks),
        `.git` internals and the state dir, and 404 when nothing is there.
        """
        if ".git" in Path(raw).parts:
            raise BrowseError(403, ".git internals are not served")
        try:
            path = self.workspace.resolve(raw)
        except ToolError as exc:
            raise BrowseError(403, str(exc)) from None
        except (OSError, RuntimeError):
            # Symlink loops: RuntimeError before Python 3.13, OSError since.
            raise BrowseError(403, f"cannot resolve path: {raw}") from None
        reason = self._blocked(path)
        if reason:
            raise BrowseError(403, reason)
        if not path.exists():
            raise BrowseError(404, f"no such file or directory: {raw}")
        return path

    def listing(self, raw: str, limit: int = LIST_MAX) -> dict[str, Any]:
        """One directory: {"path", "entries", "total", "truncated"}, directories first, then by name.

        Each entry has name, type ("dir", "file", "symlink" or "other"), size and mtime. A
        symlink also has `target` ("dir", "file", "other" or None) and `followable`, which is
        False when it points outside the workspace, into `.git` or nowhere. `.git` entries are
        left out. Raises BrowseError as `resolve` does, and 400 for a file.
        """
        path = self.resolve(raw)
        if not path.is_dir():
            raise BrowseError(400, f"not a directory: {raw}")
        entries: list[dict[str, Any]] = []
        try:
            with self.workspace.open_dir(path) as fd, os.scandir(fd) as it:
                for entry in it:
                    if entry.name == ".git":
                        continue
                    item = self._entry(path / entry.name, entry)
                    if item is not None:
                        entries.append(item)
        except ToolError as exc:
            raise BrowseError(400, str(exc)) from None
        entries.sort(key=lambda e: (e["type"] != "dir" and e.get("target") != "dir", e["name"].lower(), e["name"]))
        total = len(entries)
        return {
            "path": self.workspace.rel(path),
            "entries": entries[:limit],
            "total": total,
            "truncated": total > limit,
        }

    def _entry(self, path: Path, entry: os.DirEntry[str]) -> dict[str, Any] | None:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            return None
        item: dict[str, Any] = {"name": entry.name, "size": st.st_size, "mtime": st.st_mtime}
        if stat.S_ISLNK(st.st_mode):
            item["type"] = "symlink"
            item["target"] = None
            item["followable"] = False
            try:
                target = path.resolve()
            except (OSError, RuntimeError):  # a symlink loop
                return item
            if target.is_relative_to(self.workspace.root) and self._blocked(target) is None and target.exists():
                item["followable"] = True
                item["target"] = "dir" if target.is_dir() else "file" if target.is_file() else "other"
                item["size"] = target.stat().st_size
        elif stat.S_ISDIR(st.st_mode):
            item["type"] = "dir"
        elif stat.S_ISREG(st.st_mode):
            item["type"] = "file"
        else:
            item["type"] = "other"
        return item

    def regular_file(self, raw: str) -> Path:
        """`resolve`, then require a regular file. Raises BrowseError 400 for anything else.

        FIFOs and devices are refused: reading one could block forever or never end.
        """
        path = self.resolve(raw)
        if path.is_dir():
            raise BrowseError(400, f"is a directory: {raw}")
        if not path.is_file():
            raise BrowseError(400, f"not a regular file: {raw}")
        return path

    def open_download(self, raw: str) -> tuple[BinaryIO, str]:
        """(open binary handle, file name) of a whole file for download. The caller closes the handle.

        Opened like `read`, so a file or directory swapped after the check is refused instead of
        followed. Raises BrowseError as `regular_file` does, and 400 for a swapped path.
        """
        path = self.regular_file(raw)
        try:
            return self.workspace.open_file(path), path.name
        except ToolError as exc:
            raise BrowseError(400, str(exc)) from None

    def read(self, raw: str, offset: int = 0, limit: int = TEXT_DEFAULT) -> Chunk:
        """A preview chunk of one file. Blocking file IO.

        Text: bytes [offset, offset+limit) moved inward to whole UTF-8 characters, with
        `limit` capped at RAW_MAX. Image: the whole file when it is at most IMAGE_MAX bytes
        (offset and limit are ignored); a larger image is treated as binary. Binary: the
        byte range, unchanged. Raises BrowseError as `regular_file` does.
        """
        path = self.regular_file(raw)
        limit = max(1, min(limit, RAW_MAX))
        try:
            # Not path.open(): the file could be swapped for a FIFO (blocks forever) or a
            # directory above it for a symlink to a host directory since `regular_file` checked.
            fh = self.workspace.open_file(path)
        except ToolError as exc:
            raise BrowseError(400, str(exc)) from None
        with fh:
            size = os.fstat(fh.fileno()).st_size
            head = fh.read(_SNIFF)
            kind, mime = sniff(head, whole=len(head) >= size)
            if kind == "image" and size <= IMAGE_MAX:
                fh.seek(0)
                image = read_at_most(fh, IMAGE_MAX)
                if image is not None:
                    return Chunk("image", mime, image, 0, len(image), len(image))
            if kind == "image":
                kind, mime = "binary", "application/octet-stream"
            offset = max(0, min(offset, size))
            fh.seek(offset)
            data = fh.read(limit)
        if kind == "binary":
            return Chunk(kind, mime, data, offset, offset + len(data), size)
        start, end = _utf8_bounds(data, offset == 0, offset + len(data) >= size)
        return Chunk(kind, mime, data[start:end], offset + start, offset + end, size)
