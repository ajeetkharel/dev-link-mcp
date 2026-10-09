"""Path confinement for every tool that takes a file path from the agent."""

from __future__ import annotations

import contextlib
import os
import re
import time
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError


class Workspace:
    """The directory the agent works in.

    All agent-supplied paths go through `resolve`. When `confined` (the default, used
    with the sandbox) symlinks are followed before checking containment, so a symlink
    pointing outside the root is rejected. Unconfined (--no-sandbox) matches what the
    shell can already reach: any host path is accepted.
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
    def dev_link_dir(self) -> Path:
        """`<root>/.dev-link`: server-managed files the agent should see (artifacts, worktrees).

        Created on first use with a `.gitignore` of `*` so it never shows up in the
        user's git status.
        """
        path = self.root / ".dev-link"
        self._check_real_dir(path)
        if not path.is_dir():
            path.mkdir()
            (path / ".gitignore").write_text("*\n")
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
        directory.mkdir(exist_ok=True)
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
