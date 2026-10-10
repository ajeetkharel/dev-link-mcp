"""One-shot shell commands and code snippets, run inside the sandbox."""

import shutil
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.sandbox import RunResult
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import DESTRUCTIVE, OPEN_WORLD

Language = Literal["python", "node", "bash", "sh", "ruby", "perl", "php"]

# Each interpreter reads the program from stdin; the trailing arguments make the
# remaining argv reach the program instead of the interpreter.
_INTERPRETERS: dict[str, tuple[str, list[str]]] = {
    "python": ("python3", ["-"]),
    "node": ("node", ["-"]),
    "bash": ("bash", ["-s", "--"]),
    "sh": ("sh", ["-s", "--"]),
    "ruby": ("ruby", ["-"]),
    "perl": ("perl", ["-"]),
    "php": ("php", ["--"]),
}

_TRUNCATED_HINT = (
    "Output exceeded the cap. To see all of it, redirect to a file in the workspace"
    " (e.g. `cmd > out.log 2>&1`) and read it with read_file or grep."
)


def _check_no_nul(label: str, *values: str) -> None:
    if any("\0" in value for value in values):
        raise ToolError(f"{label} contains a NUL byte, which cannot be passed to a program")


async def _run(
    rt: Runtime,
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: float | None,
    stdin: str | None,
    env: dict[str, str] | None = None,
) -> str:
    """Run argv in the sandbox (cwd already resolved) and render the result for the agent."""
    result = await rt.sandbox.run(
        argv, cwd=cwd, timeout_s=timeout_s, stdin=stdin, env=env, max_output_bytes=rt.config.limits.max_output_bytes
    )
    return _render(result)


def _render(result: RunResult) -> str:
    out = result.format()
    if result.truncated:
        out += "\n" + _TRUNCATED_HINT
    return out


def _register(mcp: MCPServer, rt: Runtime) -> None:
    # Commands can reach the network unless the sandbox blocks it.
    annotations = OPEN_WORLD if rt.config.sandbox.network else DESTRUCTIVE
    limits = rt.config.limits

    @mcp.tool(annotations=annotations)
    async def run_command(
        command: Annotated[str, Field(description="Run with `bash -c`, so pipes, && and globs work.")],
        cwd: Annotated[str, Field(description="Working directory, relative to the workspace root.")] = ".",
        timeout_s: Annotated[
            float | None,
            Field(
                gt=0,
                description=f"Seconds before the command is killed. Default {limits.default_timeout_s:.0f},"
                f" max {limits.max_timeout_s:.0f}.",
            ),
        ] = None,
        env: Annotated[dict[str, str] | None, Field(description="Extra environment variables.")] = None,
        stdin: Annotated[str | None, Field(description="Text sent to standard input.")] = None,
    ) -> str:
        """Run a shell command and wait for it to finish. Returns the exit code, duration, stdout
        and stderr; a non-zero exit is reported in the text, not as a tool error. Use start_process
        for servers, watchers and anything long-running: background jobs (`cmd &`, nohup) are
        killed when the command returns. Sandboxed commands share a /tmp (emptied when
        the server starts) and a private HOME, both kept between calls."""
        if not command.strip():
            raise ToolError("command is empty")
        _check_no_nul("command", command)
        workdir = rt.workspace.resolve(cwd, must_exist=True)
        if not workdir.is_dir():
            raise ToolError(f"cwd is not a directory: {rt.workspace.rel(workdir)}")
        return await _run(rt, rt.sandbox.shell_argv(command), cwd=workdir, timeout_s=timeout_s, stdin=stdin, env=env)

    @mcp.tool(annotations=annotations)
    async def run_code(
        language: Language,
        code: Annotated[str, Field(description="Program source.")],
        timeout_s: Annotated[
            float | None,
            Field(
                gt=0,
                description=f"Seconds before the program is killed. Default {limits.default_timeout_s:.0f},"
                f" max {limits.max_timeout_s:.0f}.",
            ),
        ] = None,
        args: Annotated[
            list[str] | None, Field(description="Command-line arguments for the program (sys.argv[1:], $1...).")
        ] = None,
    ) -> str:
        """Run a code snippet from the workspace root without writing a file. The code is passed
        on stdin, so the program cannot read stdin. Returns the same result as run_command."""
        if not code.strip():
            raise ToolError("code is empty")
        _check_no_nul("args", *(args or []))
        program, interp_args = _INTERPRETERS[language]
        # The sandbox PATH lists only directories visible inside bubblewrap, so a host
        # lookup against it tells whether the interpreter exists in there.
        if shutil.which(program, path=rt.sandbox.path) is None:
            raise ToolError(f"{program} is not installed or not on the sandbox PATH; check workspace_info")
        argv = [program, *interp_args, *(args or [])]
        return await _run(rt, argv, cwd=rt.workspace.root, timeout_s=timeout_s, stdin=code)


class ShellGroup(ToolGroup):
    """One-shot shell commands and code snippets, run inside the sandbox."""

    name = "shell"

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt)
