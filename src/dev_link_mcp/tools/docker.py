"""Docker containers, images and compose projects through the host docker CLI.

The docker socket is root-equivalent, so commands run on the host (not in bubblewrap)
and the group is opt-in. To keep the agent inside the workspace the tools never mount
anything but the workspace, never pass --privileged or host namespaces, reject compose
files that ask for those, and only touch containers this workspace created.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from dev_link_mcp.runtime import Runtime
from dev_link_mcp.sandbox import Capture, RunResult, kill_tree, pump
from dev_link_mcp.tools.base import ToolGroup
from dev_link_mcp.tools.common import DESTRUCTIVE, OPEN_WORLD, READ_ONLY, clip

log = logging.getLogger(__name__)

LABEL = "dev-link.workspace"
_COMPOSE_DIR_LABEL = "com.docker.compose.project.working_dir"

# Only what the docker CLI needs to find the daemon, its config and plugins. Compose
# interpolates ${VAR} from this environment, so server secrets must not be in it.
_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "XDG_RUNTIME_DIR",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_BUILDKIT",
)

_UNREACHABLE = (
    "cannot connect to the docker daemon",
    "failed to connect to the docker api",
    "permission denied while trying to connect",
    "error during connect",
    "is the docker daemon running",
)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PORT = re.compile(
    r"^(?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:]+\]):)?"
    r"(?:(?P<host>\d{0,5}(?:-\d{1,5})?):)?"
    r"(?P<container>\d{1,5}(?:-\d{1,5})?)(?P<proto>/(?:tcp|udp|sctp))?$"
)

_COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
_INSPECT_TIMEOUT_S = 30.0
# `dev-link stop` sends SIGKILL 10 s after SIGTERM; uvicorn takes up to 3 s of that before the
# groups shut down, all at once.
_SHUTDOWN_TIMEOUT_S = 6.0
_SHUTDOWN_STOP_GRACE_S = 2
_MAX_COMPOSE_BYTES = 1024 * 1024
_MAX_LIST = 500
_MAX_TAIL = 10_000

ComposeAction = Literal["up", "down", "ps", "logs", "build", "restart"]


def _docker_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}


async def run_docker(
    args: Sequence[str],
    *,
    timeout_s: float,
    max_output_bytes: int,
    cwd: Path | None = None,
    stdin: bytes | None = None,
) -> RunResult:
    """Run `docker <args>` on the host with a timeout, killing the CLI on timeout or cancel.

    `stdin` is written to the CLI's standard input and then closed; None gives it /dev/null.
    Raises ToolError when the docker CLI is missing or the daemon cannot be reached, so
    callers only see results from a working daemon. A non-zero exit is returned, not raised.
    """
    exe = shutil.which("docker")
    if exe is None:
        raise ToolError("docker CLI not found on the server host. Install Docker and restart the server.")
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            cwd=str(cwd) if cwd else None,
            env=_docker_env(),
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise ToolError(f"cannot start docker: {exc}") from exc
    out, err = Capture(max_output_bytes), Capture(max_output_bytes // 4)
    timed_out = False

    async def feed() -> None:
        if stdin is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The CLI exited without reading all of it; its exit code and stderr say why.
            pass
        finally:
            proc.stdin.close()

    try:
        try:
            async with asyncio.timeout(timeout_s):
                await asyncio.gather(feed(), pump(proc.stdout, out), pump(proc.stderr, err))
                await proc.wait()
        except TimeoutError:
            timed_out = True
    finally:
        if proc.returncode is None:
            kill_tree(proc.pid)
            await proc.wait()
    result = RunResult(
        exit_code=proc.returncode,
        stdout=out.text(),
        stderr=err.text(),
        duration_s=time.monotonic() - started,
        timed_out=timed_out,
        truncated=bool(out.dropped or err.dropped),
    )
    if not result.ok and any(marker in result.stderr.lower() for marker in _UNREACHABLE):
        raise ToolError(
            "Docker daemon is unreachable. Start Docker, or check that the server user can access the"
            f" docker socket (docker group or DOCKER_HOST).\n{result.stderr.strip()}"
        )
    return result


def _check_name(value: str, what: str) -> str:
    # Leading '-' would be parsed as a docker option, so names are validated strictly.
    if not _NAME.match(value):
        raise ToolError(f"invalid {what} {value!r}: use letters, digits, '_', '.', '-' (no leading '-')")
    return value


def _check_image(value: str, what: str = "image") -> str:
    if not _IMAGE.match(value):
        raise ToolError(
            f"invalid {what} {value!r}: expected a reference like 'name', 'repo/name:tag' or 'name@sha256:...'"
        )
    return value


def _publish_arg(spec: str) -> str:
    """Normalize a port spec for `-p`, binding to 127.0.0.1 unless the agent named an address."""
    match = _PORT.match(spec.strip())
    if not match:
        raise ToolError(
            f"invalid port {spec!r}: use 'CONTAINER', 'HOST:CONTAINER' or 'IP:HOST:CONTAINER' (optional /udp)"
        )
    ip = match["ip"] or "127.0.0.1"
    host = match["host"] or ""
    return f"{ip}:{host}:{match['container']}{match['proto'] or ''}"


def _mount_arg(source: Path) -> str:
    # --mount is parsed as CSV, so quote the source field in case the path has a comma.
    quoted = '"source=' + str(source).replace('"', '""') + '"'
    return f"type=bind,{quoted},target=/workspace"


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse the comma-joined `Labels` column of `docker ps --format json`."""
    labels: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        if sep:
            labels[key] = value
    return labels


def _json_lines(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# Compose keeps adding service keys that reach the host (provider runs a host program,
# use_api_socket mounts the docker socket), so service and build keys are allowlisted:
# anything not listed here, or in the checked keys below, is refused.
_SAFE_SERVICE_KEYS = frozenset(
    {
        "annotations", "attach", "blkio_config", "command", "configs", "container_name", "cpu_count",
        "cpu_percent", "cpu_period", "cpu_quota", "cpu_rt_period", "cpu_rt_runtime", "cpu_shares", "cpus",
        "cpuset", "cap_drop", "depends_on", "deploy", "develop", "dns", "dns_opt", "dns_search", "domainname",
        "entrypoint", "environment", "expose", "external_links", "extra_hosts", "gpus", "group_add",
        "healthcheck", "hostname", "image", "init", "labels", "links", "logging", "mac_address", "mem_limit",
        "mem_reservation", "mem_swappiness", "memswap_limit", "networks", "oom_kill_disable", "oom_score_adj",
        "pids_limit", "platform", "ports", "profiles", "pull_policy", "read_only", "restart", "scale",
        "secrets", "shm_size", "stdin_open", "stop_grace_period", "stop_signal", "storage_opt", "sysctls",
        "tmpfs", "tty", "ulimits", "user", "working_dir",
    }
)  # fmt: skip
_CHECKED_SERVICE_KEYS = frozenset(
    {
        "build", "cap_add", "cgroup", "cgroup_parent", "device_cgroup_rules", "devices", "env_file", "ipc",
        "label_file", "network_mode", "pid", "post_start", "pre_stop", "privileged", "security_opt",
        "userns_mode", "uts", "volumes", "volumes_from",
    }
)  # fmt: skip
_SAFE_BUILD_KEYS = frozenset(
    {
        "args", "context", "dockerfile", "dockerfile_inline", "extra_hosts", "isolation", "labels", "no_cache",
        "platforms", "pull", "secrets", "shm_size", "tags", "target", "ulimits",
    }
)  # fmt: skip
_CHECKED_BUILD_KEYS = frozenset({"additional_contexts", "cache_from", "cache_to", "network"})
_TOP_LEVEL_KEYS = frozenset({"name", "services", "networks", "volumes", "secrets", "configs"})


def compose_violations(config: dict[str, Any], root: Path) -> list[str]:
    """Settings in a resolved compose config (`docker compose config --no-env-resolution --format json`)
    that would give containers or the compose CLI access to the host beyond the workspace, or that
    this check does not know. Empty when the file is acceptable."""

    def outside(path: str) -> bool:
        # The resolved config has absolute paths; anything else is relative to the daemon's cwd.
        return not Path(path).is_absolute() or not Path(path).resolve().is_relative_to(root)

    problems: list[str] = []
    for key in config:
        if key not in _TOP_LEVEL_KEYS and not key.startswith("x-"):
            problems.append(f"top-level key {key!r} is not supported by this server")
    for name, svc in (config.get("services") or {}).items():
        where = f"service {name!r}"
        for key in svc:
            if key not in _SAFE_SERVICE_KEYS and key not in _CHECKED_SERVICE_KEYS:
                problems.append(f"{where}: {key} is not allowed")
        if svc.get("privileged"):
            problems.append(f"{where}: privileged is not allowed")
        for key in ("network_mode", "pid", "ipc", "uts", "userns_mode"):
            value = str(svc.get(key) or "")
            if value == "host" or value.startswith("container:"):
                problems.append(f"{where}: {key}: {value} is not allowed")
        if svc.get("cgroup") not in (None, "private"):
            problems.append(f"{where}: cgroup: {svc['cgroup']} is not allowed")
        for key in ("cap_add", "devices", "device_cgroup_rules", "cgroup_parent"):
            if svc.get(key):
                problems.append(f"{where}: {key} is not allowed")
        for opt in svc.get("security_opt") or []:
            # Custom seccomp/apparmor profiles and label/systempaths overrides all loosen confinement.
            if not re.fullmatch(r"no-new-privileges([:=]true)?", str(opt)):
                problems.append(f"{where}: security_opt {opt!r} is not allowed")
        for hook in [*(svc.get("post_start") or []), *(svc.get("pre_stop") or [])]:
            if hook.get("privileged"):
                problems.append(f"{where}: privileged lifecycle hooks are not allowed")
        for ref in svc.get("volumes_from") or []:
            if str(ref).startswith("container:"):
                problems.append(f"{where}: volumes_from {ref!r} reaches a container outside the project")
        for vol in svc.get("volumes") or []:
            kind = vol.get("type")
            if kind == "bind" and outside(str(vol.get("source", ""))):
                problems.append(f"{where}: bind mount {vol.get('source')!r} is outside the workspace")
            elif kind not in ("bind", "volume", "tmpfs", "image"):
                problems.append(f"{where}: {kind} mounts are not allowed")
        for item in svc.get("env_file") or []:
            file = str(item.get("path", "") if isinstance(item, dict) else item)
            if outside(file):
                problems.append(f"{where}: env_file {file!r} is outside the workspace")
        for file in svc.get("label_file") or []:
            if outside(str(file)):
                problems.append(f"{where}: label_file {file!r} is outside the workspace")
        problems += _build_violations(where, svc.get("build") or {}, outside)
    for name, vol in (config.get("volumes") or {}).items():
        vol = vol or {}
        if vol.get("external"):
            problems.append(f"volume {name!r}: external volumes are not allowed (they belong to other projects)")
        if vol.get("driver") not in (None, "local"):
            problems.append(f"volume {name!r}: driver {vol['driver']!r} is not allowed")
        device = (vol.get("driver_opts") or {}).get("device")
        if device and outside(str(device)):
            problems.append(f"volume {name!r}: driver_opts device {device!r} is outside the workspace")
    for name, net in (config.get("networks") or {}).items():
        net = net or {}
        if net.get("name") == "host" or net.get("driver") == "host":
            problems.append(f"network {name!r}: the host network is not allowed")
    for section in ("secrets", "configs"):
        for name, item in (config.get(section) or {}).items():
            source = (item or {}).get("file")
            if source and outside(str(source)):
                problems.append(f"{section[:-1]} {name!r}: file {source!r} is outside the workspace")
    return problems


def _build_violations(where: str, build: dict[str, Any], outside: Callable[[str], bool]) -> list[str]:
    problems: list[str] = []
    for key in build:
        if key not in _SAFE_BUILD_KEYS and key not in _CHECKED_BUILD_KEYS:
            # ssh forwards host keys, privileged/entitlements lift build confinement.
            problems.append(f"{where}: build.{key} is not allowed")
    context = str(build.get("context") or "")
    local_context = bool(context) and "://" not in context and not context.startswith("git@")
    if local_context and outside(context):
        problems.append(f"{where}: build context {context!r} is outside the workspace")
    dockerfile = str(build.get("dockerfile") or "")
    if dockerfile and local_context and outside(str(Path(context) / dockerfile)):
        problems.append(f"{where}: build dockerfile {dockerfile!r} is outside the workspace")
    for ctx_name, ctx in (build.get("additional_contexts") or {}).items():
        ctx = str(ctx)
        if "://" not in ctx and not ctx.startswith(("service:", "git@", "docker-image:")) and outside(ctx):
            problems.append(f"{where}: additional build context {ctx_name!r} ({ctx}) is outside the workspace")
    if build.get("network") == "host":
        problems.append(f"{where}: build network host is not allowed")
    # type=local caches are read and written by the CLI on the host.
    for cache in [*(build.get("cache_from") or []), *(build.get("cache_to") or [])]:
        for field in str(cache).split(","):
            key, _, value = field.partition("=")
            if key in ("src", "dest") and outside(value):
                problems.append(f"{where}: build cache path {value!r} is outside the workspace")
    return problems


class DockerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    build_timeout_s: float = Field(
        default=600.0,
        gt=0,
        description="Timeout in seconds for docker_build, docker_compose and a detached docker_run (which may pull "
        "an image) when the agent does not set one. `limits.max_timeout_s` still caps it.",
    )


def _owned(labels: dict[str, str], root: Path) -> bool:
    """Whether a container with these labels was created from the workspace `root`."""
    if labels.get(LABEL) == str(root):
        return True
    # An empty or relative value would resolve against the server's cwd.
    compose_dir = Path(labels.get(_COMPOSE_DIR_LABEL) or "")
    return compose_dir.is_absolute() and compose_dir.resolve().is_relative_to(root)


class _Started:
    """Containers and compose projects that this server started in the background, for shutdown()."""

    def __init__(self) -> None:
        self.containers: set[str] = set()
        self.projects: set[str] = set()


def _register(mcp: MCPServer, rt: Runtime, settings: DockerSettings, started: _Started) -> None:
    build_timeout_s = settings.build_timeout_s
    limits = rt.config.limits
    root = rt.workspace.root
    workspace_label = f"{LABEL}={root}"

    async def docker(
        args: Sequence[str],
        *,
        timeout_s: float = _INSPECT_TIMEOUT_S,
        cwd: Path | None = None,
        stdin: bytes | None = None,
    ) -> RunResult:
        return await run_docker(
            args, timeout_s=timeout_s, max_output_bytes=limits.max_output_bytes, cwd=cwd, stdin=stdin
        )

    def timeout(value: float | None, default: float | None = None) -> float:
        return rt.sandbox.clamp_timeout(value if value else default)

    def fail(what: str, result: RunResult) -> ToolError:
        return ToolError(f"{what} failed\n{clip(result.format(), limits.max_output_bytes)}")

    def owned(labels: dict[str, str]) -> bool:
        return _owned(labels, root)

    async def own_container(container: str) -> tuple[str, bool]:
        """Resolve a container name or id to its full id and running state.

        Raises ToolError when it does not exist or was not created from this workspace,
        so the agent cannot stop, exec into or read other host containers.
        """
        _check_name(container, "container")
        result = await docker(
            [
                "container",
                "inspect",
                "--format",
                "{{json .Id}} {{json .State.Running}} {{json .Config.Labels}}",
                container,
            ]
        )
        if not result.ok:
            raise ToolError(f"no such container: {container} (see docker_ps)")
        cid, running, labels = result.stdout.strip().split(" ", 2)
        if not owned(json.loads(labels) or {}):
            raise ToolError(
                f"container {container} was not started from this workspace (docker_run or compose);"
                " only those containers can be managed"
            )
        return json.loads(cid), json.loads(running)

    async def own_project(project: str) -> None:
        """Raise ToolError when existing containers of compose project `project` were not created
        from this workspace, so a compose file cannot reuse another project's name to control it."""
        if not project:
            raise ToolError("compose config did not report a project name")
        result = await docker(
            [
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                f'{{{{json .Names}}}}\t{{{{json (.Label "{LABEL}")}}}}\t{{{{json (.Label "{_COMPOSE_DIR_LABEL}")}}}}',
            ]
        )
        if not result.ok:
            raise fail("docker ps", result)
        for line in result.stdout.splitlines():
            name, label, compose_dir = (json.loads(part) for part in line.split("\t"))
            if not owned({LABEL: label, _COMPOSE_DIR_LABEL: compose_dir}):
                raise ToolError(
                    f"compose project {project!r} already has container {name} from outside this workspace;"
                    " set a different top-level `name:` in the compose file"
                )

    def compose_file(file: str | None) -> Path:
        if file:
            path = rt.workspace.resolve(file, must_exist=True)
            if not path.is_file():
                raise ToolError(f"compose file is not a file: {rt.workspace.rel(path)}")
            return path
        for name in _COMPOSE_FILES:
            if (root / name).is_file():
                return rt.workspace.resolve(name, must_exist=True)
        raise ToolError(f"no compose file found in the workspace root ({', '.join(_COMPOSE_FILES)}); pass file")

    @mcp.tool(annotations=READ_ONLY)
    async def docker_ps(
        all: Annotated[bool, Field(description="Include stopped containers.")] = False,
    ) -> dict[str, Any]:
        """List host containers with id, name, image, state, status and ports. `workspace` is true
        for containers started from this workspace; only those can be used with docker_logs,
        docker_exec, docker_stop and docker_rm."""
        args = ["ps", "--no-trunc", "--format", "{{json .}}"] + (["--all"] if all else [])
        result = await docker(args)
        if not result.ok:
            raise fail("docker ps", result)
        rows = _json_lines(result.stdout)
        containers = [
            {
                "id": row.get("ID", "")[:12],
                "name": row.get("Names", ""),
                "image": row.get("Image", ""),
                "state": row.get("State", ""),
                "status": row.get("Status", ""),
                "ports": row.get("Ports", ""),
                "workspace": owned(_parse_labels(row.get("Labels", ""))),
            }
            for row in rows[:_MAX_LIST]
        ]
        return {"containers": containers, "count": len(rows), "truncated": len(rows) > _MAX_LIST}

    @mcp.tool(annotations=READ_ONLY)
    async def docker_images() -> dict[str, Any]:
        """List local images with repository, tag, id, size and age."""
        result = await docker(["images", "--format", "{{json .}}"])
        if not result.ok:
            raise fail("docker images", result)
        rows = _json_lines(result.stdout)
        images = [
            {
                "repository": row.get("Repository", ""),
                "tag": row.get("Tag", ""),
                "id": row.get("ID", ""),
                "size": row.get("Size", ""),
                "created": row.get("CreatedSince", ""),
            }
            for row in rows[:_MAX_LIST]
        ]
        return {"images": images, "count": len(rows), "truncated": len(rows) > _MAX_LIST}

    @mcp.tool(annotations=OPEN_WORLD)
    async def docker_build(
        tag: Annotated[str, Field(description="Image tag to create, e.g. 'myapp:dev'.")],
        context: Annotated[str, Field(description="Build context directory, relative to the workspace root.")] = ".",
        dockerfile: Annotated[
            str | None, Field(description="Dockerfile path inside the workspace. Default: <context>/Dockerfile.")
        ] = None,
        timeout_s: Annotated[
            float | None,
            Field(
                gt=0,
                description=f"Seconds before the build is cancelled. Default {build_timeout_s:.0f}"
                f" (capped at {limits.max_timeout_s:.0f}).",
            ),
        ] = None,
    ) -> str:
        """Build an image from a Dockerfile in the workspace; base images may be pulled. Returns the
        build log (clipped); a failed build is a tool error carrying the log."""
        _check_image(tag, "tag")
        ctx = rt.workspace.resolve(context, must_exist=True)
        if not ctx.is_dir():
            raise ToolError(f"build context is not a directory: {rt.workspace.rel(ctx)}")
        args = ["build", "--progress", "plain", "--label", workspace_label, "--tag", tag]
        if dockerfile:
            df = rt.workspace.resolve(dockerfile, must_exist=True)
            if not df.is_file():
                raise ToolError(f"dockerfile is not a file: {rt.workspace.rel(df)}")
            args += ["--file", str(df)]
        elif not (ctx / "Dockerfile").is_file():
            raise ToolError(f"no Dockerfile in {rt.workspace.rel(ctx)}; pass dockerfile")
        result = await docker([*args, str(ctx)], timeout_s=timeout(timeout_s, build_timeout_s), cwd=root)
        if not result.ok:
            raise fail(f"docker build of {tag}", result)
        return f"built {tag}\n{clip(result.format(), limits.max_output_bytes)}"

    @mcp.tool(annotations=OPEN_WORLD)
    async def docker_run(
        image: Annotated[str, Field(description="Image to run, e.g. 'postgres:16' or a tag from docker_build.")],
        command: Annotated[
            list[str] | None,
            Field(description="Argv overriding the image CMD, e.g. ['sh', '-c', 'npm test']. Default: image CMD."),
        ] = None,
        name: Annotated[str | None, Field(description="Container name. Default: generated by docker.")] = None,
        ports: Annotated[
            list[str] | None,
            Field(
                description="Published ports: 'CONTAINER', 'HOST:CONTAINER' or 'IP:HOST:CONTAINER', optional"
                " '/udp'. Bound to 127.0.0.1 unless an IP is given."
            ),
        ] = None,
        env: Annotated[dict[str, str] | None, Field(description="Environment variables for the container.")] = None,
        detach: Annotated[
            bool, Field(description="Run in the background and return the id. False waits for the container to exit.")
        ] = True,
        mount_workspace: Annotated[
            bool, Field(description="Bind-mount the workspace at /workspace (read-write) and start there.")
        ] = True,
        timeout_s: Annotated[
            float | None,
            Field(
                gt=0,
                description=f"With detach=False: seconds before the container is killed and removed."
                f" Default {limits.default_timeout_s:.0f}, max {limits.max_timeout_s:.0f}.",
            ),
        ] = None,
    ) -> str:
        """Start a container labeled as belonging to this workspace, pulling the image if missing.
        The workspace is the only host mount allowed; privileged mode and host namespaces are not
        available. detach=True returns the container id and published ports; detach=False waits and
        returns the exit code and output, like run_command, and removes the container. Detached
        containers are stopped (not removed) when the server stops."""
        _check_image(image)
        args = ["run", "--label", workspace_label]
        if name:
            args += ["--name", _check_name(name, "container name")]
        elif not detach:
            # A killed `docker run` client leaves the container running, so a foreground run
            # needs a known name to clean up after a timeout or cancellation.
            name = f"dev-link-{secrets.token_hex(4)}"
            args += ["--name", name]
        # A foreground run is over when the tool returns, so its exited container is only clutter.
        args.append("--detach" if detach else "--rm")
        if mount_workspace:
            args += ["--mount", _mount_arg(root), "--workdir", "/workspace"]
        for spec in ports or []:
            args += ["--publish", _publish_arg(spec)]
        for key, value in (env or {}).items():
            if not _ENV_NAME.match(key):
                raise ToolError(f"invalid environment variable name: {key!r}")
            if "\0" in value:
                raise ToolError(f"environment variable {key} contains a NUL byte")
            args += ["--env", f"{key}={value}"]
        args += ["--", image, *(command or [])]
        if detach:
            result = await docker(args, timeout_s=timeout(None, build_timeout_s))
            if not result.ok:
                raise fail(f"docker run {image}", result)
            full_id = result.stdout.strip().splitlines()[-1]
            started.containers.add(full_id)
            cid = full_id[:12]
            port_info = await docker(["port", cid])
            lines = [f"started container {cid}" + (f" ({name})" if name else "") + f" from {image}"]
            published = port_info.stdout.strip() if port_info.ok else ""
            lines.append("ports:\n" + published if published else "ports: none published")
            return "\n".join(lines)
        assert name is not None
        try:
            result = await docker(args, timeout_s=timeout(timeout_s))
        except asyncio.CancelledError:
            await asyncio.shield(docker(["rm", "--force", name]))
            raise
        if result.timed_out:
            await docker(["rm", "--force", name])
            return result.format() + f"\ncontainer {name} was killed and removed"
        if result.exit_code == 125:
            raise fail(f"docker run {image}", result)
        return clip(result.format(), limits.max_output_bytes) + f"\ncontainer {name} exited and was removed"

    @mcp.tool(annotations=READ_ONLY)
    async def docker_logs(
        container: Annotated[str, Field(description="Container name or id started from this workspace.")],
        tail: Annotated[int, Field(ge=1, le=_MAX_TAIL, description="Number of most recent lines.")] = 200,
    ) -> str:
        """Recent stdout and stderr of a container started from this workspace (docker_run or
        compose), with timestamps."""
        cid, _ = await own_container(container)
        result = await docker(["logs", "--timestamps", "--tail", str(tail), cid])
        if not result.ok:
            raise fail(f"docker logs {container}", result)
        parts = []
        if result.stdout:
            parts.append("--- stdout ---\n" + result.stdout.rstrip("\n"))
        if result.stderr:
            parts.append("--- stderr ---\n" + result.stderr.rstrip("\n"))
        return clip("\n".join(parts) or "(no output)", limits.max_output_bytes)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def docker_exec(
        container: Annotated[str, Field(description="Running container name or id started from this workspace.")],
        command: Annotated[str, Field(description="Shell command, run with `sh -c` inside the container.")],
        timeout_s: Annotated[
            float | None,
            Field(
                gt=0,
                description=f"Seconds before the command is killed. Default {limits.default_timeout_s:.0f},"
                f" max {limits.max_timeout_s:.0f}.",
            ),
        ] = None,
    ) -> str:
        """Run a shell command inside a running container started from this workspace. The image must
        have `sh`. Returns exit code, stdout and stderr; a non-zero exit is reported in the text. On timeout
        only the docker client is killed; the command may keep running in the container."""
        if not command.strip():
            raise ToolError("command is empty")
        cid, running = await own_container(container)
        if not running:
            raise ToolError(f"container {container} is not running")
        result = await docker(["exec", cid, "sh", "-c", command], timeout_s=timeout(timeout_s))
        return clip(result.format(), limits.max_output_bytes)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def docker_stop(
        container: Annotated[str, Field(description="Container name or id started from this workspace.")],
    ) -> str:
        """Stop a running container started from this workspace with `docker stop`. The container is
        kept; remove it with docker_rm."""
        cid, _ = await own_container(container)
        result = await docker(["stop", cid])
        if not result.ok:
            raise fail(f"docker stop {container}", result)
        return f"stopped {container}"

    @mcp.tool(annotations=DESTRUCTIVE)
    async def docker_rm(
        container: Annotated[str, Field(description="Container name or id started from this workspace.")],
        force: Annotated[bool, Field(description="Kill and remove the container even if it is running.")] = False,
    ) -> str:
        """Remove a container started from this workspace. Fails on a running container unless
        force is set. Named volumes are kept."""
        cid, _ = await own_container(container)
        result = await docker(["rm", *(["--force"] if force else []), cid])
        if not result.ok:
            raise fail(f"docker rm {container}", result)
        return f"removed {container}"

    @mcp.tool(annotations=OPEN_WORLD)
    async def compose(
        action: Annotated[ComposeAction, Field(description="docker compose subcommand to run.")],
        file: Annotated[
            str | None,
            Field(
                description="Compose file inside the workspace. Default: compose.yaml / docker-compose.yml"
                " in the workspace root."
            ),
        ] = None,
        services: Annotated[list[str] | None, Field(description="Limit to these services. Default: all.")] = None,
        detach: Annotated[
            bool,
            Field(
                description="For up: start in the background. False waits until the containers exit; on timeout"
                " compose is killed and the containers keep running (use down)."
            ),
        ] = True,
        tail: Annotated[int, Field(ge=1, le=_MAX_TAIL, description="For logs: lines per service.")] = 200,
        timeout_s: Annotated[
            float | None,
            Field(
                gt=0,
                description=f"Seconds before compose is killed. Default {build_timeout_s:.0f}"
                f" (capped at {limits.max_timeout_s:.0f}).",
            ),
        ] = None,
    ) -> str:
        """Run docker compose for a compose file in the workspace. The resolved config is refused, with
        each problem listed, if it reaches outside the workspace (privileged, host namespaces, cap_add,
        devices, external volumes, paths outside the workspace, unknown keys, ...) or its project name
        belongs to containers from elsewhere. down keeps named volumes. Containers started with up or
        restart are stopped (not removed) when the server stops. A failing command is a tool error
        carrying compose's output."""
        path = compose_file(file)
        for svc in services or []:
            _check_name(svc, "service")
        # The file is read once and goes to the check over stdin; --project-directory keeps relative
        # paths and .env as they would be with --file.
        try:
            with rt.workspace.open_file(path) as fh:
                content = fh.read(_MAX_COMPOSE_BYTES + 1)
        except OSError as exc:
            raise ToolError(f"cannot read compose file {rt.workspace.rel(path)}: {exc}") from exc
        if len(content) > _MAX_COMPOSE_BYTES:
            raise ToolError(f"compose file is larger than {_MAX_COMPOSE_BYTES} bytes: {rt.workspace.rel(path)}")
        base = ["compose", "--ansi", "never", "--progress", "plain", "--file", "-"]
        base += ["--project-directory", str(path.parent)]
        # Every action is checked: provider services run a host program even on down, and the
        # project name (from the file or .env) could point at another project's containers.
        # --no-env-resolution keeps env_file paths in the output instead of inlining their contents.
        checked = await docker(
            [*base, "config", "--no-env-resolution", "--format", "json"], cwd=path.parent, stdin=content
        )
        if not checked.ok:
            raise fail("compose config", checked)
        config = json.loads(checked.stdout)
        problems = compose_violations(config, root)
        if problems:
            raise ToolError("compose file refused:\n- " + "\n- ".join(problems))
        project = str(config.get("name") or "")
        await own_project(project)
        base += ["--project-name", project]
        sub: list[str] = {
            "up": ["up", *(["--detach"] if detach else [])],
            "down": ["down"],
            "ps": ["ps", "--all"],
            "logs": ["logs", "--no-color", "--timestamps", "--tail", str(tail)],
            "build": ["build"],
            "restart": ["restart"],
        }[action]
        if action in ("up", "restart"):
            # Recorded before running, so containers from an up that times out are stopped too.
            started.projects.add(project)
        result = await docker(
            [*base, *sub, *(services or [])],
            timeout_s=timeout(timeout_s, build_timeout_s),
            cwd=path.parent,
            # The resolved config that passed the check, not the file: .env, include and extends
            # are read again by path and could be swapped in between. Compose keeps literal $ escaped
            # as $$ in this output, so nothing is substituted twice.
            stdin=checked.stdout.encode(),
        )
        if not result.ok:
            raise fail(f"compose {action}", result)
        return f"compose {action} ({rt.workspace.rel(path)})\n{clip(result.format(), limits.max_output_bytes)}"


class DockerGroup(ToolGroup):
    """Docker containers, images and compose projects through the host docker CLI."""

    name = "docker"
    # The docker daemon is root-equivalent and outside the bubblewrap sandbox.
    opt_in = True
    Settings = DockerSettings
    settings: DockerSettings

    def __init__(self, rt: Runtime, settings: BaseModel) -> None:
        super().__init__(rt, settings)
        self._started = _Started()

    def register(self, mcp: MCPServer) -> None:
        _register(mcp, self.rt, self.settings, self._started)

    async def shutdown(self) -> None:
        """Stop the detached containers and compose projects this server started, like background
        processes are killed when the server stops. Containers are kept (so are volumes) and can be
        started again. Never raises; failures and the time limit are logged as warnings."""
        if not self._started.containers and not self._started.projects:
            return
        try:
            async with asyncio.timeout(_SHUTDOWN_TIMEOUT_S):
                await self._stop_started()
        except (ToolError, OSError, TimeoutError) as exc:
            log.warning("could not stop docker containers started by this server: %s", exc)

    async def _stop_started(self) -> None:
        root = self.rt.workspace.root
        max_bytes = self.rt.config.limits.max_output_bytes
        ids = set(self._started.containers)
        for project in sorted(self._started.projects):
            listed = await run_docker(
                [
                    "ps",
                    "--no-trunc",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--filter",
                    f"label={_COMPOSE_DIR_LABEL}",
                    "--format",
                    f'{{{{.ID}}}}\t{{{{.Label "{_COMPOSE_DIR_LABEL}"}}}}',
                ],
                timeout_s=_SHUTDOWN_TIMEOUT_S,
                max_output_bytes=max_bytes,
            )
            for line in listed.stdout.splitlines():
                cid, _, compose_dir = line.partition("\t")
                # Same ownership rule as the tools: another project may have taken the name since.
                if _owned({_COMPOSE_DIR_LABEL: compose_dir}, root):
                    ids.add(cid)
        if not ids:
            return
        # A container removed in the meantime makes docker stop exit non-zero; the others still stop.
        result = await run_docker(
            ["stop", "--time", str(_SHUTDOWN_STOP_GRACE_S), *sorted(ids)],
            timeout_s=_SHUTDOWN_TIMEOUT_S,
            max_output_bytes=max_bytes,
        )
        if not result.ok and "no such container" not in result.stderr.lower():
            log.warning("docker stop at shutdown failed: %s", result.stderr.strip())
