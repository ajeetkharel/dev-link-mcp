"""Project detection and the standard install/test/lint/format/build commands.

`project_detect` reads manifests (package.json, pyproject.toml, Makefile, pom.xml, ...)
without running anything. The run_* tools pick the detected command for their kind,
or an explicit `command`, and run it in the sandbox. `run_tests` also turns the test
runner's output (or fresh JUnit XML reports) into pass/fail counts.
"""

import asyncio
import shlex
import time
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.sandbox import RunResult
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import DESTRUCTIVE, OPEN_WORLD, READ_ONLY, clip
from dev_link_mcp.tools.project.detect import detect_project
from dev_link_mcp.tools.project.parsers import parse_junit_reports, parse_test_output


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Installs and full test or build runs often take minutes.
    long_timeout_s: float = Field(
        default=600.0,
        gt=0,
        description="Timeout in seconds for install_deps, run_tests and run_build when the agent does not set one. "
        "`limits.max_timeout_s` still caps it.",
    )


def _register(mcp: MCPServer, rt: Runtime, settings: ProjectSettings) -> None:
    long_timeout_s = settings.long_timeout_s
    # Installs, builds and test suites run project code and usually download packages.
    run_annotations = OPEN_WORLD if rt.config.sandbox.network else DESTRUCTIVE
    limits = rt.config.limits

    def project_dir(path: str) -> Path:
        root = rt.workspace.resolve(path, must_exist=True)
        if not root.is_dir():
            raise ToolError(f"path is not a directory: {rt.workspace.rel(root)}")
        return root

    async def resolve_command(root: Path, command: str | None, kind: str) -> str:
        if command is not None:
            if not command.strip():
                raise ToolError("command is empty; omit it to use the detected command")
            return command
        det = await asyncio.to_thread(detect_project, root, rt.workspace)
        chosen = det.commands().get(kind)
        if chosen is None:
            found = ", ".join(det.manifests) or "none"
            raise ToolError(
                f"no {kind.replace('_', ' ')} command detected in {rt.workspace.rel(root)} (manifests: {found})."
                " Pass `command` explicitly, or call project_detect to see what was found."
            )
        return chosen

    async def execute(root: Path, command: str, timeout_s: float | None) -> RunResult:
        return await rt.sandbox.run(
            rt.sandbox.shell_argv(command),
            cwd=root,
            timeout_s=timeout_s,
            max_output_bytes=limits.max_output_bytes,
        )

    def render(root: Path, command: str, result: RunResult) -> str:
        out = f"$ {command}\n(cwd: {rt.workspace.rel(root)})\n{result.format()}"
        if result.truncated:
            out += "\nOutput exceeded the cap; rerun with run_command and redirect to a file to read all of it."
        return out

    def timeout_field(default: float | None) -> Any:
        shown = default if default is not None else limits.default_timeout_s
        return Field(gt=0, description=f"Seconds before the command is killed. Default {shown:.0f},"
                     f" max {limits.max_timeout_s:.0f}.")  # fmt: skip

    path_field = Field(description="Project directory, relative to the workspace root.")

    def command_field(what: str) -> Any:
        return Field(description=f"Shell command to run instead of the detected {what} command.")

    @mcp.tool(annotations=READ_ONLY)
    async def project_detect(path: Annotated[str, path_field] = ".") -> dict[str, Any]:
        """Inspect a project directory without running anything: languages, manifests, package
        managers, frameworks, package.json scripts, Makefile targets, just recipes, compose
        services, subprojects and the detected commands. `commands` holds the one per kind that
        the run_* tools and install_deps use (Makefile/justfile first); `commands_by_manifest`
        lists every candidate."""
        root = project_dir(path)
        det = await asyncio.to_thread(detect_project, root, rt.workspace)
        return {"path": rt.workspace.rel(root), **det.to_dict()}

    @mcp.tool(annotations=run_annotations)
    async def install_deps(
        path: Annotated[str, path_field] = ".",
        command: Annotated[str | None, command_field("install")] = None,
        timeout_s: Annotated[float, timeout_field(long_timeout_s)] = long_timeout_s,
    ) -> str:
        """Install dependencies with the detected command (npm install, uv sync, go mod download,
        cargo fetch, mvn install, bundle install, ...) or `command`. Returns the command, exit code
        and output."""
        root = project_dir(path)
        cmd = await resolve_command(root, command, "install")
        return render(root, cmd, await execute(root, cmd, timeout_s))

    @mcp.tool(annotations=run_annotations)
    async def run_tests(
        path: Annotated[str, path_field] = ".",
        command: Annotated[str | None, command_field("test")] = None,
        target: Annotated[
            str | None,
            Field(
                description="One test file, directory, package or name to run (e.g. tests/test_api.py::test_x,"
                " ./pkg/..., com.acme.AppTest). Inserted into the detected command, or appended to `command`."
            ),
        ] = None,
        timeout_s: Annotated[float, timeout_field(long_timeout_s)] = long_timeout_s,
    ) -> dict[str, Any]:
        """Run the tests with the detected command or `command`. Fresh JUnit XML reports, or the
        output of pytest, jest, vitest, mocha, go test, cargo, maven, gradle, dotnet, rspec, phpunit
        or unittest, become passed/failed/skipped counts and a failures list of {name, message};
        counts are null when no format is recognized. The raw output is always included. Failing
        tests are a normal result, not a tool error."""
        root = project_dir(path)
        if target is not None and not target.strip():
            raise ToolError("target is empty; omit it to run the whole suite")
        if command is not None:
            cmd = await resolve_command(root, command, "test")
            if target:
                cmd = f"{cmd} {shlex.quote(target)}"
        elif target:
            det = await asyncio.to_thread(detect_project, root, rt.workspace)
            template = det.commands().get("test_target")
            if template is None:
                detected = det.commands().get("test", "none")
                raise ToolError(
                    f"cannot add a target to the detected test command ({detected}). Pass the full"
                    " command, e.g. command='pytest tests/test_x.py'."
                )
            cmd = template.replace("{target}", shlex.quote(target))
        else:
            cmd = await resolve_command(root, None, "test")
        # Report files keep whole-second mtimes on some filesystems.
        started = time.time() - 1
        result = await execute(root, cmd, timeout_s)
        output = result.stdout + "\n" + result.stderr
        confine = rt.workspace.root if rt.workspace.confined else None
        summary = await asyncio.to_thread(parse_junit_reports, root, confine, started)
        if summary is None:
            summary = await asyncio.to_thread(parse_test_output, output)
        return {
            "command": cmd,
            "cwd": rt.workspace.rel(root),
            "ok": result.ok,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_s": round(result.duration_s, 2),
            "summary_format": summary.format if summary else None,
            "passed": summary.passed if summary else None,
            "failed": summary.failed if summary else None,
            "skipped": summary.skipped if summary else None,
            "failures": summary.failures if summary else [],
            "output_truncated": result.truncated,
            "output": clip(result.format(), limits.max_output_bytes),
        }

    @mcp.tool(annotations=run_annotations)
    async def run_lint(
        path: Annotated[str, path_field] = ".",
        command: Annotated[str | None, command_field("lint")] = None,
        fix: Annotated[bool, Field(description="Run the linter's auto-fix variant (edits files).")] = False,
        timeout_s: Annotated[float | None, timeout_field(None)] = None,
    ) -> str:
        """Run the linter with the detected command (ruff, eslint, go vet, clippy, rubocop, a
        Makefile or package.json lint target, ...) or `command`. Returns the command, exit code
        and output."""
        root = project_dir(path)
        cmd = await resolve_command(root, command, "lint_fix" if fix else "lint")
        return render(root, cmd, await execute(root, cmd, timeout_s))

    @mcp.tool(annotations=run_annotations)
    async def run_format(
        path: Annotated[str, path_field] = ".",
        command: Annotated[str | None, command_field("format")] = None,
        check: Annotated[
            bool, Field(description="Only report files that need formatting; do not change them.")
        ] = False,
        timeout_s: Annotated[float | None, timeout_field(None)] = None,
    ) -> str:
        """Run the formatter with the detected command (ruff format, black, prettier, gofmt, cargo
        fmt, ...) or `command`. Rewrites files unless check is set. Returns the command, exit code
        and output."""
        root = project_dir(path)
        cmd = await resolve_command(root, command, "format_check" if check else "format")
        return render(root, cmd, await execute(root, cmd, timeout_s))

    @mcp.tool(annotations=run_annotations)
    async def run_build(
        path: Annotated[str, path_field] = ".",
        command: Annotated[str | None, command_field("build")] = None,
        timeout_s: Annotated[float, timeout_field(long_timeout_s)] = long_timeout_s,
    ) -> str:
        """Build the project with the detected command (npm run build, go build ./..., cargo
        build, mvn package, uv build, make build, ...) or `command`. Returns the command, exit
        code and output."""
        root = project_dir(path)
        cmd = await resolve_command(root, command, "build")
        return render(root, cmd, await execute(root, cmd, timeout_s))


class ProjectGroup(ToolGroup):
    """Project detection and the standard install/test/lint/format/build commands."""

    name = "project"
    Settings = ProjectSettings
    settings: ProjectSettings

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt, self.settings)
