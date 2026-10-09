"""Shared handles passed to every tool module."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from dev_link_mcp.audit import AuditStore
from dev_link_mcp.config import Config
from dev_link_mcp.processes import LOG_DIR, ProcessManager
from dev_link_mcp.sandbox import Sandbox
from dev_link_mcp.workspace import Workspace

log = logging.getLogger(__name__)


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
        files in the state dir: the caller must be the only server using it.

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
                pruned = audit.prune(days, config.state_dir / LOG_DIR)
            except (OSError, sqlite3.Error) as exc:
                # Housekeeping only: a full disk must not stop a server that would otherwise start.
                log.warning("could not prune old audit data, will retry at the next start: %s", exc)
            else:
                if pruned.events or pruned.jsonl_lines or pruned.process_logs:
                    log.info(
                        "removed audit data older than %d days: %d events, %d JSONL lines, %d process logs",
                        days,
                        pruned.events,
                        pruned.jsonl_lines,
                        pruned.process_logs,
                    )
        processes = ProcessManager(sandbox, audit, config.state_dir, config.limits.max_processes)
        return cls(
            config=config,
            workspace=Workspace(config.workspace, confined=config.sandbox.enabled),
            sandbox=sandbox,
            audit=audit,
            processes=processes,
        )
