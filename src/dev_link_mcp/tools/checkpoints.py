"""Workspace snapshots in a shadow git repository the agent cannot see.

The repository lives in the server state dir with the workspace as its work tree, so
checkpoints never touch the user's own `.git`. Host `git` runs directly instead of
through the sandbox because the state dir is deliberately hidden from the sandbox.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import DESTRUCTIVE, READ_ONLY, WRITE, clip

# Dependency and build output dirs: large, regenerable, and slow to snapshot.
# Workspace .gitignore files apply on top of this list.
_EXCLUDES = (
    ".git",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "dist/",
    "build/",
    "target/",
    ".gradle/",
    ".next/",
    ".cache/",
)

_ID_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")
_GITLINK_MODE = "160000"
_MAX_LISTED_PATHS = 200
_MAX_SKIPPED_LINES = 20


def _printable(text: str) -> str:
    """Agent-facing form of git output: undecodable filename bytes become U+FFFD."""
    return text.encode(errors="surrogateescape").decode(errors="replace")


class _ShadowRepo:
    """Runs host git against the shadow repository with user and system config disabled."""

    def __init__(self, rt: Runtime):
        assert rt.config.state_dir is not None
        self.rt = rt
        self.git_dir = rt.config.state_dir / "checkpoints.git"
        self.work_tree = rt.workspace.root
        # One git operation at a time: concurrent `add`/`commit` calls would fight over index.lock.
        self.lock = asyncio.Lock()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            GIT_DIR=str(self.git_dir),
            GIT_WORK_TREE=str(self.work_tree),
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_LITERAL_PATHSPECS="1",
            GIT_TERMINAL_PROMPT="0",
            LC_ALL="C",
        )

    async def git(self, *args: str, stdin: bytes | None = None, check: bool = True) -> tuple[int, str, str]:
        """Run `git <args>` in the work tree. Returns (exit code, stdout, stderr).

        Stdout is decoded with surrogateescape so non-UTF-8 filenames survive a round trip back
        to git or the filesystem; pass it through `_printable` before showing it to the agent.
        Raises ToolError on timeout, or on a non-zero exit when `check` is set. The process is
        killed if the call times out or the tool call is cancelled.
        """
        argv = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.excludesFile=/dev/null",
            "-c", "core.quotePath=false",
            "-c", "core.autocrlf=false",
            "-c", "commit.gpgSign=false",
            "-c", "gc.auto=0",
            "-c", "user.name=dev-link-mcp",
            "-c", "user.email=dev-link-mcp@localhost",
            *args,
        ]  # fmt: skip
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.work_tree,
                env=self.env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise ToolError("checkpoints need the `git` executable on the server host's PATH") from None
        timeout = self.rt.config.limits.default_timeout_s
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ToolError(f"checkpoint git {args[0]} timed out after {timeout:.0f}s (workspace too large?)") from None
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        code = proc.returncode if proc.returncode is not None else -1
        stdout, stderr = out.decode(errors="surrogateescape"), err.decode(errors="replace")
        if check and code != 0:
            raise ToolError(f"checkpoint git {args[0]} failed: {stderr.strip() or stdout.strip()}")
        return code, stdout, stderr

    async def ensure(self) -> None:
        """Create the shadow repository and its exclude file on first use."""
        if not (self.git_dir / "HEAD").exists():
            await self.git("init", "-q")
        exclude = self.git_dir / "info" / "exclude"
        content = "\n".join(_EXCLUDES) + "\n"
        if not exclude.exists() or exclude.read_text() != content:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(content)

    async def has_commits(self) -> bool:
        code, _, _ = await self.git("rev-parse", "--verify", "-q", "HEAD", check=False)
        return code == 0

    async def resolve_id(self, checkpoint_id: str) -> str:
        """Full commit hash for a checkpoint id (full or abbreviated hash)."""
        checkpoint_id = checkpoint_id.strip()
        if not _ID_RE.match(checkpoint_id):
            raise ToolError(f"invalid checkpoint id {checkpoint_id!r}: use an id from checkpoint_list")
        code, out, _ = await self.git("rev-parse", "--verify", "-q", f"{checkpoint_id}^{{commit}}", check=False)
        if code != 0:
            raise ToolError(f"unknown checkpoint id {checkpoint_id!r}: use an id from checkpoint_list")
        return out.strip()

    async def stage(self) -> list[str]:
        """Stage every non-ignored file into the shadow index. Caller holds the lock.

        Unreadable files and nested repos without a commit would abort a plain `add`, so they
        are skipped and returned as git's error lines for the agent to see.
        """
        code, _, err = await self.git("add", "-A", "--ignore-errors", ".", check=False)
        if code not in (0, 1):
            raise ToolError(f"checkpoint git add failed: {err.strip()}")
        return [line for line in err.splitlines() if line.startswith("error:")][:_MAX_SKIPPED_LINES]

    async def snapshot(self, message: str) -> dict[str, Any]:
        """Stage every non-ignored file and commit it. Caller holds the lock."""
        skipped = await self.stage()
        first = not await self.has_commits()
        changed = True
        if not first:
            code, _, _ = await self.git("diff", "--cached", "--quiet", "HEAD", check=False)
            changed = code != 0
        stat = ""
        if not first and changed:
            _, stat, _ = await self.git("diff", "--cached", "--shortstat", "HEAD")
        # Via stdin: a long message would exceed the kernel's per-argument size limit.
        await self.git("commit", "-q", "--allow-empty", "--no-verify", "-F", "-", stdin=message.encode())
        _, out, _ = await self.git("log", "-1", "--format=%h%x1f%cI")
        short, date = out.strip().split("\x1f")
        _, files, _ = await self.git("ls-files", "-z")
        result: dict[str, Any] = {
            "id": short,
            "message": message,
            "date": date,
            "tracked_files": len([f for f in files.split("\0") if f]),
            "changes_since_previous": "first checkpoint" if first else (stat.strip() or "no changes"),
        }
        if skipped:
            result["not_snapshotted"] = skipped
        return result


def _pathspecs(rt: Runtime, paths: list[str] | None) -> list[str]:
    """Workspace-relative git pathspecs for agent paths; empty means the whole workspace."""
    if paths is None:
        return []
    specs = []
    for raw in paths:
        rel = rt.workspace.rel(rt.workspace.resolve(raw))
        if rel == ".":
            return []
        specs.append(rel)
    return specs


def _delete_files(rt: Runtime, rel_paths: list[str]) -> list[str]:
    """Remove files git reported as added since a checkpoint, then any directories left empty.

    The parent dir is resolved through Workspace, not the file itself, so a tracked symlink
    is unlinked rather than followed. Unlinks go through a descriptor of the parent, so a
    directory swapped for a symlink meanwhile raises ToolError instead of deleting host files.
    """
    ws = rt.workspace
    root = ws.root
    deleted = []
    for rel in rel_paths:
        target = Path(rel)
        parent = ws.resolve(str(target.parent) if str(target.parent) else ".")
        if not parent.is_dir():
            continue
        with ws.dir_fd(parent) as parent_fd:
            try:
                mode = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            if not (stat.S_ISLNK(mode) or stat.S_ISREG(mode)):
                continue
            os.unlink(target.name, dir_fd=parent_fd)
        deleted.append(rel)
        # Walk upwards removing directories this deletion emptied.
        current = parent
        while current != root and current.is_relative_to(root):
            try:
                with ws.dir_fd(current.parent) as up:
                    os.rmdir(current.name, dir_fd=up)
            except (OSError, ToolError):
                break
            current = current.parent
    return deleted


def _conflicts(root: Path, rel_paths: list[str]) -> set[str]:
    """Checkpoint files that git checkout could only write by deleting something untracked.

    After added files are deleted, anything still occupying a file's path (a directory) or one of
    its parent dirs (a file or symlink) is ignored content or a nested repo, which the auto
    checkpoint does not hold.
    """
    blocked: set[str] = set()
    checked: dict[str, bool] = {}
    for rel in rel_paths:
        parts = rel.split("/")
        target = root.joinpath(*parts)
        if target.is_dir() and not target.is_symlink():
            blocked.add(rel)
            continue
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if prefix not in checked:
                path = root / prefix
                checked[prefix] = path.is_symlink() or (path.exists() and not path.is_dir())
            if checked[prefix]:
                blocked.add(rel)
                break
    return blocked


def _register(mcp: MCPServer, rt: Runtime) -> None:
    repo = _ShadowRepo(rt)
    limit = rt.config.limits.max_output_bytes

    @mcp.tool(annotations=WRITE)
    async def checkpoint_create(
        message: Annotated[str, Field(description="Short label for this snapshot, e.g. 'before refactor'")] = "",
    ) -> dict[str, Any]:
        """Snapshot the workspace files into a private checkpoint store, separate from the project's
        git history. Skips .gitignore'd files, dependency/build dirs (node_modules, .venv, dist, build,
        target, ...) and nested git repositories. Returns the checkpoint id, a summary of changes since
        the previous one, and `not_snapshotted` errors for unreadable files."""
        message = message.strip() or "checkpoint"
        if "\0" in message:
            raise ToolError("message must not contain NUL characters")
        async with repo.lock:
            await repo.ensure()
            return await repo.snapshot(message)

    @mcp.tool(annotations=READ_ONLY)
    async def checkpoint_list(
        max_count: Annotated[int, Field(ge=1, le=1000, description="Most recent checkpoints to return")] = 50,
    ) -> dict[str, Any]:
        """List checkpoints, newest first, with id, date and message. Each restore adds an automatic
        'auto: before restore' checkpoint."""
        async with repo.lock:
            await repo.ensure()
            if not await repo.has_commits():
                return {"checkpoints": [], "note": "no checkpoints yet; create one with checkpoint_create"}
            _, out, _ = await repo.git("log", f"--max-count={max_count}", "--format=%h%x1f%cI%x1f%s")
        checkpoints = []
        for line in out.splitlines():
            short, date, subject = line.split("\x1f", 2)
            checkpoints.append({"id": short, "date": date, "message": _printable(subject)})
        return {"checkpoints": checkpoints}

    @mcp.tool(annotations=READ_ONLY)
    async def checkpoint_diff(
        id: Annotated[str, Field(description="Checkpoint id from checkpoint_list")],
        path: Annotated[str | None, Field(description="Limit the diff to this file or directory")] = None,
        stat_only: Annotated[bool, Field(description="Only list changed files with line counts")] = False,
    ) -> str:
        """Show how the current workspace files differ from a checkpoint, as a unified diff
        ('-' lines are the checkpoint, '+' lines are now), including files created or deleted since.
        Clipped at the output cap; narrow it with `path` or stat_only."""
        specs = _pathspecs(rt, None if path is None else [path])
        async with repo.lock:
            await repo.ensure()
            sha = await repo.resolve_id(id)
            # Staging the current files makes new, not-yet-tracked files show up in the diff.
            await repo.stage()
            args = ["diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames"]
            if stat_only:
                args.append("--stat=200")
            _, out, _ = await repo.git(*args, sha, "--", *specs)
        if not out.strip():
            scope = f" under {specs[0]}" if specs else ""
            return f"no differences between checkpoint {id} and the current files{scope}"
        out = _printable(out)
        clipped = clip(out, limit)
        if clipped != out:
            clipped += "\n(diff truncated: pass `path` to narrow it or stat_only=true for a summary)"
        return clipped

    @mcp.tool(annotations=DESTRUCTIVE)
    async def checkpoint_restore(
        id: Annotated[str, Field(description="Checkpoint id from checkpoint_list")],
        paths: Annotated[
            list[str] | None,
            Field(description="Restore only these files or directories; omit to restore the whole workspace"),
        ] = None,
    ) -> dict[str, Any]:
        """Return workspace files to their state at a checkpoint: overwrites changed files, recreates
        deleted ones and deletes files created since. Ignored files and dependency/build dirs are left
        alone; checkpoint files they now block are listed in `not_restored`. An automatic checkpoint is
        taken first, so restoring `auto_checkpoint` undoes the restore."""
        if paths is not None and not paths:
            raise ToolError("paths is empty: omit it to restore everything, or list files/directories")
        specs = _pathspecs(rt, paths)
        async with repo.lock:
            await repo.ensure()
            sha = await repo.resolve_id(id)
            auto = await repo.snapshot(f"auto: before restore to {id}")
            _, raw, _ = await repo.git(
                "diff", "--raw", "-z", "--no-renames", "--diff-filter=A", sha, "HEAD", "--", *specs
            )
            # -z --raw output alternates ":<old mode> <new mode> <sha> <sha> A" and the path.
            fields = raw.split("\0")
            added = [
                fields[i + 1]
                for i in range(0, len(fields) - 1, 2)
                if fields[i].split(" ")[1] != _GITLINK_MODE  # nested repos are never deleted
            ]
            _, listing, _ = await repo.git("ls-tree", "-r", "-z", "--name-only", sha, "--", *specs)
            restore = [f for f in listing.split("\0") if f]
            if specs and not added and not restore:
                raise ToolError(f"checkpoint {id} has no files under {', '.join(specs)} and none were added since")
            deleted = await asyncio.to_thread(_delete_files, rt, added)
            blocked = await asyncio.to_thread(_conflicts, rt.workspace.root, restore)
            restore = [f for f in restore if f not in blocked]
            if restore:
                await repo.git(
                    "checkout", sha, "--pathspec-from-file=-", "--pathspec-file-nul",
                    stdin="\0".join(restore).encode(errors="surrogateescape"),
                )  # fmt: skip
        result: dict[str, Any] = {
            "restored_to": id,
            "auto_checkpoint": auto["id"],
            "restored_files": len(restore),
            "deleted_files": [_printable(f) for f in deleted[:_MAX_LISTED_PATHS]],
            "deleted_count": len(deleted),
            "note": f"undo with checkpoint_restore(id={auto['id']!r})",
        }
        if blocked:
            result["not_restored"] = [_printable(f) for f in sorted(blocked)[:_MAX_LISTED_PATHS]]
            result["not_restored_reason"] = (
                "untracked content (ignored files or a nested git repo) now occupies these paths; "
                "move it away and restore them again"
            )
        return result


class CheckpointsGroup(ToolGroup):
    """Workspace snapshots in a shadow git repository the agent cannot see."""

    name = "checkpoints"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)
