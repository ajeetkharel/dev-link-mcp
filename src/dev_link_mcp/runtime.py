"""Shared handles passed to every tool module."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field
from pathlib import Path

from dev_link_mcp.audit import AuditStore
from dev_link_mcp.config import Config
from dev_link_mcp.processes import LOG_DIR, ProcessManager
from dev_link_mcp.sandbox import Sandbox
from dev_link_mcp.workspace import Workspace

log = logging.getLogger(__name__)

# Retention is counted in whole days, so one pass a day keeps nothing more than a day past it.
# Each pass that drops data rewrites the JSONL and deletes rows in batches, which briefly holds up
# audit writes each time, so it should not run much more often.
PRUNE_INTERVAL_S = 86400.0


@dataclass
class Runtime:
    config: Config
    workspace: Workspace
    sandbox: Sandbox
    audit: AuditStore
    processes: ProcessManager
    # Group name -> install hint for enabled groups whose packages are missing; set by the tool registry.
    unavailable_groups: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, config: Config) -> Runtime:
        """Open the audit store and sandbox for a finalized config.

        Prunes audit events and process logs older than `audit.retention_days`, which rewrites
        files in the state dir: the caller must be the only server using it. While the server
        runs, `prune_periodically` keeps doing so.

        Raises SandboxUnavailable when the sandbox backend or a configured resource limit
        cannot work on this machine.
        """
        assert config.state_dir is not None
        # First, so a sandbox that cannot run stops the server before anything is opened.
        sandbox = Sandbox(config)
        audit = AuditStore(config.state_dir, secrets=[config.server.token], result_bytes=config.audit.result_bytes)
        audit.mark_interrupted()
        days = config.audit.retention_days
        if days is not None:
            try:
                # Nothing records yet, so this is the one pass that can also VACUUM.
                _prune(audit, days, config.state_dir / LOG_DIR, (), vacuum=True)
            except (OSError, sqlite3.Error) as exc:
                # Housekeeping only: a full disk must not stop a server that would otherwise start.
                log.warning("could not prune old audit data, will retry at the next start: %s", exc)
        processes = ProcessManager(sandbox, audit, config.state_dir, config.limits.max_processes)
        return cls(
            config=config,
            workspace=Workspace(config.workspace, confined=config.sandbox.enabled),
            sandbox=sandbox,
            audit=audit,
            processes=processes,
        )

    @contextlib.asynccontextmanager
    async def prune_periodically(self, interval_s: float = PRUNE_INTERVAL_S) -> AsyncIterator[None]:
        """Prune old audit data every `interval_s` seconds while the block runs; no-op without retention.

        Each pass runs in a worker thread and keeps the logs of every process the ProcessManager
        still lists, since their output can still be read. A failed pass logs a warning and the
        next one tries again. Leaving the block stops the schedule and waits for a pass in
        progress, so the audit store can be closed right after.
        """
        days = self.config.audit.retention_days
        if days is None:
            yield
            return
        assert self.config.state_dir is not None
        log_dir = self.config.state_dir / LOG_DIR
        current: asyncio.Task[None] | None = None

        async def schedule() -> None:
            nonlocal current
            while True:
                await asyncio.sleep(interval_s)
                try:
                    keep = self.processes.log_paths()
                    current = asyncio.create_task(asyncio.to_thread(_prune, self.audit, days, log_dir, keep))
                    # Shielded: a thread cannot be stopped, so cancelling must not abandon one mid-rewrite.
                    await asyncio.shield(current)
                except Exception as exc:
                    log.warning("could not prune old audit data, will retry in %.0f s: %s", interval_s, exc)

        task = asyncio.create_task(schedule(), name="audit retention")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if current is not None:
                with contextlib.suppress(Exception):
                    await current


def _prune(audit: AuditStore, days: int, log_dir: Path, keep: Collection[Path], *, vacuum: bool = False) -> None:
    """Run AuditStore.prune and log what it removed. Raises like AuditStore.prune."""
    pruned = audit.prune(days, log_dir, keep, vacuum=vacuum)
    if pruned.events or pruned.jsonl_lines or pruned.process_logs:
        log.info(
            "removed audit data older than %d days: %d events, %d JSONL lines, %d process logs",
            days,
            pruned.events,
            pruned.jsonl_lines,
            pruned.process_logs,
        )
