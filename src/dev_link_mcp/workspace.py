"""Path confinement for every tool that takes a file path from the agent."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from mcp.server.mcpserver.exceptions import ToolError

# Each directory below the root is opened with O_NOFOLLOW, so a symlink fails the open instead of
# being followed. O_PATH needs no read permission and is enough to act as a dir_fd.
_WALK_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SWAPPED = (errno.ENOTDIR, errno.ELOOP)


def _shown(path: Path, beneath: Path | None) -> str:
    if beneath is not None and path.is_relative_to(beneath):
        return str(path.relative_to(beneath)) or "."
    return str(path)


def _with_path(exc: OSError, path: Path) -> OSError:
    """The same OS error naming the full path instead of the single component an *at() call saw."""
    return type(exc)(exc.errno, exc.strerror, str(path))


@contextlib.contextmanager
def _naming(directory: Path) -> Iterator[None]:
    """Report OS errors from *at() calls in the block with the full path, not the bare name."""
    try:
        yield
    except OSError as exc:
        if isinstance(exc.filename, str) and exc.filename and not os.path.isabs(exc.filename):
            raise _with_path(exc, directory / exc.filename) from exc
        raise


def _open_dir(directory: Path, beneath: Path | None, create: bool) -> int:
    if beneath is None:
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return os.open(directory, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    if not directory.is_relative_to(beneath):
        raise ToolError(f"path is outside the workspace: {directory}")
    parts = directory.relative_to(beneath).parts
    fd = os.open(beneath, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for depth, name in enumerate(parts, 1):
            here = beneath.joinpath(*parts[:depth])
            try:
                try:
                    child = os.open(name, _WALK_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(name, dir_fd=fd)
                    child = os.open(name, _WALK_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in _SWAPPED:
                    raise ToolError(
                        f"{_shown(here, beneath)} is not a directory, or was replaced by a symlink while in use"
                    ) from exc
                raise _with_path(exc, here) from exc
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def dir_fd(directory: Path, beneath: Path | None, *, create: bool = False) -> Iterator[int]:
    """O_PATH descriptor of `directory`, held open for the `with` block, for use as a dir_fd.

    With `beneath` (a resolved root), the walk starts at the root and refuses every symlink
    below it, so a directory swapped for a symlink after the path was checked cannot redirect
    the operation outside. `directory` must already be resolved: legitimate symlinks are
    followed by `Workspace.resolve`, not here. Without `beneath` the path is opened as given.
    `create` makes missing directories like `mkdir -p`.

    Raises ToolError when a component is a symlink or not a directory (confined only), or
    when `directory` is not under `beneath`; other OS errors propagate with the full path.
    OS errors raised inside the block that name a relative file get `directory` prepended.
    """
    fd = _open_dir(directory, beneath, create)
    try:
        with _naming(directory):
            yield fd
    finally:
        os.close(fd)


def open_regular(path: Path, beneath: Path | None) -> BinaryIO:
    """Open a regular file for binary reading, bound to the directory walk described in `dir_fd`.

    With `beneath`, the file itself must not be a symlink either. FIFOs, sockets and devices are
    refused without blocking. Raises ToolError for those and for symlinks; the caller closes the
    returned file.
    """
    if beneath is not None and path == beneath:
        raise ToolError(". is not a regular file")
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_NOFOLLOW if beneath is not None else 0)
    with dir_fd(path.parent, beneath) as parent:
        try:
            fd = os.open(path.name, flags, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP and beneath is not None:
                raise ToolError(f"{_shown(path, beneath)} was replaced by a symlink while in use") from exc
            raise _with_path(exc, path) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ToolError(f"{_shown(path, beneath)} is not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _write_all(handle: BinaryIO, data: bytes | BinaryIO) -> None:
    if isinstance(data, bytes):
        handle.write(data)
    else:
        shutil.copyfileobj(data, handle)


def read_at_most(f: BinaryIO, limit: int) -> bytes | None:
    """The rest of `f`, or None when it holds more than `limit` bytes.

    An fstat size check alone is not a cap: the agent can grow the file between the fstat and
    the read, for example to a huge sparse file.
    """
    data = f.read(limit + 1)
    return None if len(data) > limit else data


class Workspace:
    """The directory the agent works in.

    All agent-supplied paths go through `resolve`. When `confined` (the default, used
    with the sandbox) symlinks are followed before checking containment, so a symlink
    pointing outside the root is rejected. Unconfined (--no-sandbox) matches what the
    shell can already reach: any host path is accepted.

    Sandboxed processes can swap a checked directory for a symlink before the server
    opens it, so host-side file access goes through `dir_fd`, `open_file`, `write_file`
    and `make_dirs`. When confined they walk the resolved path from the root without
    following any symlink, and fail with ToolError if one appeared since `resolve`.
    """

    def __init__(self, root: Path, *, confined: bool = True):
        self.root = root.resolve()
        self.confined = confined

    def resolve(self, path: str | os.PathLike[str] = ".", *, must_exist: bool = False) -> Path:
        """Map an agent path (relative to the root, or absolute) to a real host path.

        Raises ToolError if confined and the path escapes the workspace, or if `must_exist`
        is set and nothing is there. The returned path may not exist yet otherwise.
        """
        raw = str(path).strip() or "."
        if "\0" in raw:
            raise ToolError("path contains a NUL byte")
        candidate = Path(raw)
        if raw.startswith("~"):
            # `~name` for an unknown user stays a literal file name.
            with contextlib.suppress(RuntimeError):
                candidate = candidate.expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        # strict=False resolves symlinks for the parts that exist and keeps the rest literal.
        resolved = candidate.resolve(strict=False)
        if self.confined and not resolved.is_relative_to(self.root):
            raise ToolError(f"path is outside the workspace: {raw}")
        if must_exist and not resolved.exists():
            raise ToolError(f"path does not exist: {self.rel(resolved)}")
        return resolved

    @property
    def beneath(self) -> Path | None:
        """Root that `dir_fd` and `open_regular` must not leave: the root when confined, else None."""
        return self.root if self.confined else None

    def dir_fd(self, directory: Path, *, create: bool = False) -> contextlib.AbstractContextManager[int]:
        """`dir_fd(directory, self.beneath)`: a descriptor for a resolved directory; see that function."""
        return dir_fd(directory, self.beneath, create=create)

    def open_file(self, path: Path) -> BinaryIO:
        """Open a resolved regular file for binary reading without following a swapped-in symlink.

        Raises ToolError for symlinks (when confined) and non-regular files; OSError otherwise.
        """
        return open_regular(path, self.beneath)

    @contextlib.contextmanager
    def open_dir(self, directory: Path) -> Iterator[int]:
        """Readable descriptor of a resolved directory, for `os.scandir(fd)` and *at() calls.

        Opened through `dir_fd`, so the same symlink rules apply. Closed when the block exits;
        DirEntry objects from `os.scandir(fd)` must not be used after that.
        """
        with self.dir_fd(directory) as pinned:
            fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=pinned)
        try:
            with _naming(directory):
                yield fd
        finally:
            os.close(fd)

    def make_dirs(self, directory: Path) -> None:
        """`mkdir -p` for a resolved directory, refusing symlinks on the way when confined."""
        with self.dir_fd(directory, create=True):
            pass

    def write_file(self, path: Path, data: bytes | BinaryIO) -> bool:
        """Write `data` (bytes, or a binary file read from its current position) to a resolved file
        path whose parent directory exists.

        A missing file is created exclusively. An existing one is replaced atomically through a
        temp file in the same directory and keeps its permission bits. Returns True when an
        existing file was replaced. Raises ToolError if the path is a directory or (when
        confined) a symlink; OSError for other failures.
        """
        name = path.name
        with self.dir_fd(path.parent) as parent:
            try:
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            except OSError as exc:
                raise _with_path(exc, path) from exc
            create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            if st is None:
                try:
                    fd = os.open(name, create, 0o666, dir_fd=parent)
                except OSError as exc:
                    raise _with_path(exc, path) from exc
                with os.fdopen(fd, "wb") as handle:
                    _write_all(handle, data)
                return False
            if stat.S_ISDIR(st.st_mode):
                raise ToolError(f"{self.rel(path)} is a directory")
            if stat.S_ISLNK(st.st_mode) and self.confined:
                raise ToolError(f"{self.rel(path)} was replaced by a symlink while in use")
            tmp = f".{name}.{secrets.token_hex(4)}.tmp"
            fd = os.open(tmp, create, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    _write_all(handle, data)
                    os.fchmod(handle.fileno(), stat.S_IMODE(st.st_mode))
                os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=parent)
                raise
            return True

    @property
    def dev_link_dir(self) -> Path:
        """`<root>/.dev-link`: server-managed files the agent should see (artifacts, worktrees).

        Created on first use with a `.gitignore` of `*` so it never shows up in the
        user's git status.
        """
        path = self.root / ".dev-link"
        self._check_real_dir(path)
        if not path.is_dir():
            self.make_dirs(path)
            self.write_file(path / ".gitignore", b"*\n")
        return path

    def _check_real_dir(self, path: Path) -> None:
        """Raise ToolError if a server-managed directory is a symlink or a non-directory.

        The agent can create `.dev-link` itself from the shell; a symlink there would make the
        server write screenshots and worktrees wherever it points on the host.
        """
        if self.confined and path.is_symlink():
            raise ToolError(f"{self.rel(path)} is a symlink; remove it so the server can create a real directory")
        if path.exists() and not path.is_dir():
            raise ToolError(f"{self.rel(path)} exists and is not a directory; remove or rename it")

    def artifact_path(self, stem: str, suffix: str) -> Path:
        """New unique file path under `.dev-link/artifacts` for screenshots and other outputs.

        `stem` is sanitized to a safe filename; a timestamp keeps names unique and sortable.
        Raises ToolError if `.dev-link` or `.dev-link/artifacts` is a symlink or a file.
        """
        directory = self.dev_link_dir / "artifacts"
        self._check_real_dir(directory)
        self.make_dirs(directory)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:60] or "artifact"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        candidate = directory / f"{stamp}-{safe}{suffix}"
        counter = 1
        while candidate.exists():
            counter += 1
            candidate = directory / f"{stamp}-{safe}-{counter}{suffix}"
        return candidate

    def rel(self, path: Path) -> str:
        """Workspace-relative display form of a resolved path ('.' for the root)."""
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return str(path)
        return str(relative) or "."
