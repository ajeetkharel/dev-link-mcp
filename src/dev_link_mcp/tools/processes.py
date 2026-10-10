"""Background processes: dev servers, watchers, REPLs and other long-running commands."""

from __future__ import annotations

import asyncio
import codecs
import re
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from dev_link_mcp.patterns import PatternStuck
from dev_link_mcp.processes import ProcessExited
from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import DESTRUCTIVE, OPEN_WORLD, READ_ONLY, clip

# Without a wait condition, give the process a moment so the reply shows its first output
# (or an immediate crash) instead of an empty log.
_SETTLE_S = 0.5

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_NAME = 80


def _check_pattern(pattern: str | None) -> None:
    if pattern is None:
        return
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regex {pattern!r}: {exc}") from None


def _check_port(port: int | None) -> None:
    if port is not None and not 1 <= port <= 65535:
        raise ToolError(f"port must be between 1 and 65535, got {port}")


def _check_start_args(command: str, name: str | None, env: dict[str, str] | None) -> None:
    """Reject input the OS cannot pass to exec, and names that would break the one-line status format."""
    if not command.strip():
        raise ToolError("command is empty")
    if "\0" in command:
        raise ToolError("command contains a NUL byte")
    if name is not None and (len(name) > _MAX_NAME or not name.isprintable()):
        raise ToolError(f"name must be at most {_MAX_NAME} printable characters without newlines")
    for key, value in (env or {}).items():
        if not _ENV_NAME.match(key):
            raise ToolError(f"invalid environment variable name: {key!r} (use letters, digits and _)")
        if "\0" in value:
            raise ToolError(f"environment variable {key} contains a NUL byte")


def _wait_target(pattern: str | None, port: int | None) -> str:
    targets = [f"pattern {pattern!r}"] if pattern is not None else []
    if port is not None:
        targets.append(f"port {port}")
    return " or ".join(targets) or "the process to exit"


def _read_utf8_chunk(path: Path, offset: int, max_bytes: int, final: bool) -> tuple[str, int, bool]:
    """Decode up to `max_bytes` log bytes from `offset` without splitting a UTF-8 character.

    A partial character at the end of the chunk is left for the next read, so paging with
    next_offset never garbles multi-byte text. `final` decodes it anyway (as U+FFFD) once the
    process has exited and no more bytes can complete it. Returns (text, next_offset, more).
    """
    if not path.exists():
        return "", 0, False
    size = path.stat().st_size
    offset = min(offset, size)
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(max_bytes)
    more = offset + len(data) < size
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data, final=final and not more)
    pending = len(decoder.getstate()[0])
    return text, offset + len(data) - pending, more


def _session(ctx: Context | None) -> str | None:
    headers = ctx.headers if ctx is not None else None
    return headers.get("mcp-session-id") if headers is not None else None


def _register(mcp: MCPServer, rt: Runtime) -> None:
    limits = rt.config.limits

    def summary(process_id: str) -> dict[str, Any]:
        """Process summary with the working directory shown workspace-relative."""
        info = rt.processes.get(process_id).summary()
        info["cwd"] = rt.workspace.rel(Path(info["cwd"]))
        return info

    def status_line(process_id: str) -> str:
        info = summary(process_id)
        return f"id={info['id']} name={info['name']} pid={info['pid']} status={info['status']} cwd={info['cwd']}"

    def output_since(process_id: str, offset: int) -> tuple[str, int, bool]:
        """(text, next_offset, more_remaining) for log bytes from `offset`, capped at the output limit."""
        managed = rt.processes.get(process_id)
        return _read_utf8_chunk(managed.log_path, offset, limits.max_output_bytes, final=not managed.running)

    async def wait(process_id: str, pattern: str | None, port: int | None, timeout_s: float) -> str:
        """ProcessManager.wait_for with a timeout message that names what was awaited."""
        rt.processes.get(process_id)  # an unknown id must surface as such, not as a timeout
        try:
            return await rt.processes.wait_for(process_id, pattern=pattern, port=port, timeout_s=timeout_s)
        except ProcessExited as exc:
            tail, _ = rt.processes.read_log(process_id, tail_lines=20)
            raise ToolError(f"{exc}; last output:\n{tail or '(no output)'}") from None
        except PatternStuck:
            raise
        except ToolError:
            raise ToolError(
                f"timed out after {timeout_s:g}s waiting for {_wait_target(pattern, port)};"
                " the process is still running"
            ) from None

    # Background commands can reach the network unless the sandbox blocks it, same as run_command.
    @mcp.tool(annotations=OPEN_WORLD if rt.config.sandbox.network else DESTRUCTIVE)
    async def start_process(
        command: Annotated[str, Field(description="Shell command run with bash -c, e.g. 'npm run dev'.")],
        name: Annotated[str | None, Field(description="Short label shown in list_processes.")] = None,
        cwd: Annotated[str, Field(description="Working directory, relative to the workspace root.")] = ".",
        env: Annotated[dict[str, str] | None, Field(description="Extra environment variables.")] = None,
        wait_for_pattern: Annotated[
            str | None, Field(description="Regex; return once the output matches it (e.g. 'listening on').")
        ] = None,
        wait_for_port: Annotated[
            int | None, Field(description="Return once this TCP port accepts connections on 127.0.0.1 or ::1.")
        ] = None,
        wait_timeout_s: Annotated[
            float, Field(description="Max seconds to wait for the pattern or port.", gt=0)
        ] = 30.0,
        ctx: Context | None = None,
    ) -> str:
        """Start a long-running command (dev server, watcher, REPL) as a background process,
        sandboxed like run_command, and return its id and the output so far. Can first wait for a
        regex in the output or an open port; a wait timeout is reported, not raised, and the
        process keeps running. Background processes are killed
        when the server stops."""
        _check_start_args(command, name, env)
        _check_pattern(wait_for_pattern)
        _check_port(wait_for_port)
        workdir = rt.workspace.resolve(cwd, must_exist=True)
        if not workdir.is_dir():
            raise ToolError(f"cwd is not a directory: {rt.workspace.rel(workdir)}")
        timeout = min(wait_timeout_s, limits.max_timeout_s)
        managed = await rt.processes.start(command, cwd=workdir, name=name, env=env, session=_session(ctx))

        if wait_for_pattern is not None or wait_for_port is not None:
            try:
                waited = await wait(managed.id, wait_for_pattern, wait_for_port, timeout)
            except ToolError as exc:
                waited = str(exc)
        else:
            await asyncio.sleep(_SETTLE_S)
            waited = None

        text, next_offset, more = output_since(managed.id, 0)
        lines = [status_line(managed.id)]
        if waited:
            lines.append(f"wait: {waited}")
        lines.append(f"next_offset={next_offset}" + (" (more output available; call process_output)" if more else ""))
        lines.append("--- output ---")
        lines.append(text.rstrip("\n") or "(no output yet)")
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    async def list_processes() -> dict[str, Any]:
        """List background processes, running and exited, with id, name, command, cwd, pid,
        status, uptime and log size. Only the 50 most recently exited processes are kept."""
        return {"processes": [summary(p["id"]) for p in rt.processes.list()]}

    @mcp.tool(annotations=READ_ONLY)
    async def process_output(
        id: Annotated[str, Field(description="Process id from start_process, e.g. 'p1'.")],
        offset: Annotated[
            int,
            Field(description="Byte offset to read from; pass the previous next_offset to get only new output.", ge=0),
        ] = 0,
        tail_lines: Annotated[
            int | None, Field(description="Return only the last N lines instead of reading from offset.", ge=1)
        ] = None,
    ) -> str:
        """Read a background process's combined stdout and stderr, with next_offset for the next
        call. Each call returns at most the output cap and says when more remains."""
        if tail_lines is not None:
            text, next_offset = rt.processes.read_log(id, tail_lines=tail_lines, max_bytes=limits.max_output_bytes)
            text = clip(text, limits.max_output_bytes)
            more = False
        else:
            text, next_offset, more = output_since(id, offset)
        header = f"{status_line(id)}\nnext_offset={next_offset}"
        if more:
            header += " (more output available; call again with offset=next_offset)"
        return f"{header}\n--- output ---\n{text.rstrip(chr(10)) or '(no new output)'}"

    @mcp.tool(annotations=DESTRUCTIVE)
    async def process_input(
        id: Annotated[str, Field(description="Process id from start_process.")],
        text: Annotated[str, Field(description="Text written to the process's stdin.")],
        newline: Annotated[bool, Field(description="Append a newline, like pressing Enter.")] = True,
    ) -> str:
        """Write text to a running background process's stdin (answer a prompt, drive a REPL).
        Read the reply with process_output. Refused while the process has more than
        1 MiB of earlier input still unread."""
        payload = text + "\n" if newline else text
        await rt.processes.send_input(id, payload)
        return f"sent {len(payload.encode())} bytes to {id}"

    @mcp.tool(annotations=DESTRUCTIVE)
    async def stop_process(
        id: Annotated[str, Field(description="Process id from start_process.")],
    ) -> str:
        """Stop a background process and everything it started: SIGTERM, then SIGKILL after 5s.
        Returns the exit code and the last 20 lines of output. Safe to call on an exited process."""
        code = await rt.processes.stop(id)
        tail, _ = rt.processes.read_log(id, tail_lines=20)
        return f"{status_line(id)}\nexit_code={code}\n--- last output ---\n{tail or '(no output)'}"

    @mcp.tool(annotations=READ_ONLY)
    async def wait_for_process(
        id: Annotated[str, Field(description="Process id from start_process.")],
        pattern: Annotated[str | None, Field(description="Regex to wait for in the output.")] = None,
        port: Annotated[int | None, Field(description="TCP port to wait for on 127.0.0.1 or ::1.")] = None,
        timeout_s: Annotated[float, Field(description="Max seconds to wait.", gt=0)] = 30.0,
    ) -> str:
        """Wait until a background process's output matches a regex, a port accepts connections,
        or the process exits, whichever comes first. Fails on timeout; the process keeps running."""
        _check_pattern(pattern)
        _check_port(port)
        waited = await wait(id, pattern, port, min(timeout_s, limits.max_timeout_s))
        return f"{waited}\n{status_line(id)}"


class ProcessesGroup(ToolGroup):
    """Background processes: dev servers, watchers, REPLs and other long-running commands."""

    name = "processes"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)
