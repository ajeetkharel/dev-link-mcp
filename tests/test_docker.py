import ipaddress
import json
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from conftest import DOCKER_IMAGE, text

from dev_link_mcp.sandbox import RunResult
from dev_link_mcp.tools import docker as docker_mod

IMAGE = DOCKER_IMAGE


def sh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


@pytest.fixture
def cleanup(workspace):
    """Remove every container and image labeled with this test's workspace afterwards."""
    yield
    label = f"label=dev-link.workspace={workspace.resolve()}"
    ids = sh("ps", "-aq", "--filter", label).stdout.split()
    if ids:
        sh("rm", "-f", *ids)
    images = sh("images", "-q", "--filter", label).stdout.split()
    if images:
        sh("rmi", "-f", *images)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "outside"
    path.mkdir()
    (path / "Dockerfile").write_text(f"FROM {IMAGE}\n")
    (path / "compose.yaml").write_text(f"services:\n  a:\n    image: {IMAGE}\n")
    return path


# --- pure helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("80", "127.0.0.1::80"),
        ("8080:80", "127.0.0.1:8080:80"),
        ("8080:80/udp", "127.0.0.1:8080:80/udp"),
        ("0.0.0.0:8080:80", "0.0.0.0:8080:80"),
        ("[::1]:8080:80", "[::1]:8080:80"),
        ("8000-8001:8000-8001", "127.0.0.1:8000-8001:8000-8001"),
    ],
)
def test_publish_arg_defaults_to_loopback(spec, expected):
    assert docker_mod._publish_arg(spec) == expected


@pytest.mark.parametrize("spec", ["", "abc", "80:80:80:80", "-p", "80/icmp", "host:80"])
def test_publish_arg_rejects_bad_specs(spec):
    with pytest.raises(docker_mod.ToolError):
        docker_mod._publish_arg(spec)


def test_mount_arg_quotes_commas():
    assert docker_mod._mount_arg(Path("/a,b")) == 'type=bind,"source=/a,b",target=/workspace'


def test_compose_violations_flags_host_access(workspace):
    root = workspace.resolve()
    config = {
        "services": {
            "bad": {
                "privileged": True,
                "network_mode": "host",
                "pid": "host",
                "ipc": "host",
                "cap_add": ["SYS_ADMIN"],
                "devices": [{"source": "/dev/kvm"}],
                "security_opt": ["seccomp:unconfined"],
                "volumes_from": ["container:other"],
                "volumes": [
                    {"type": "bind", "source": "/etc", "target": "/x"},
                    {"type": "bind", "source": str(root / "data"), "target": "/data"},
                    {"type": "volume", "source": "named", "target": "/n"},
                ],
                "build": {"context": "/tmp", "additional_contexts": {"o": "/etc", "s": "service:base"}},
            }
        },
        "volumes": {"named": {"driver_opts": {"device": "/", "o": "bind"}}},
        "secrets": {"s": {"file": "/etc/passwd"}},
    }
    problems = "\n".join(docker_mod.compose_violations(config, root))
    for expected in (
        "privileged",
        "network_mode",
        "pid: host",
        "ipc: host",
        "cap_add",
        "devices",
        "seccomp",
        "container:other",
        "'/etc' is outside",
        "build context '/tmp'",
        "additional build context 'o'",
        "volume 'named'",
        "secret 's'",
    ):
        assert expected in problems
    assert "/data" not in problems
    assert "'s' (service:base)" not in problems


def test_compose_violations_accepts_workspace_only_config(workspace):
    root = workspace.resolve()
    config = {
        "services": {
            "web": {
                "image": "nginx",
                "volumes": [{"type": "bind", "source": str(root / "site"), "target": "/usr/share/nginx/html"}],
                "build": {"context": str(root)},
                "security_opt": ["no-new-privileges:true"],
            }
        }
    }
    assert docker_mod.compose_violations(config, root) == []


def test_compose_violations_rejects_symlink_escape(workspace, outside):
    (workspace / "link").symlink_to(outside)
    config = {"services": {"a": {"volumes": [{"type": "bind", "source": str(workspace / "link"), "target": "/x"}]}}}
    assert docker_mod.compose_violations(config, workspace.resolve())


def test_parse_labels():
    assert docker_mod._parse_labels("a=1,b=x=y,c=") == {"a": "1", "b": "x=y", "c": ""}
    assert docker_mod._parse_labels("") == {}


# --- errors that need no daemon ------------------------------------------------------------


async def test_missing_docker_cli_is_clear_error(connect, monkeypatch):
    monkeypatch.setattr(docker_mod.shutil, "which", lambda _name: None)
    async with connect("docker") as client:
        result = await client.call_tool("docker_ps", {})
    assert result.is_error
    assert "docker CLI not found" in text(result)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
async def test_unreachable_daemon_is_clear_error(connect, monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path}/missing.sock")
    async with connect("docker") as client:
        result = await client.call_tool("docker_images", {})
    assert result.is_error
    assert "Docker daemon is unreachable" in text(result)


async def test_docker_env_drops_server_secrets(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cr3t")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    env = docker_mod._docker_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"


@pytest.mark.parametrize("context", ["../outside", "{outside}", "link"])
@pytest.mark.bwrap
async def test_build_context_must_stay_in_workspace(connect, workspace, outside, context):
    (workspace / "link").symlink_to(outside)
    async with connect("docker") as client:
        result = await client.call_tool("docker_build", {"tag": "x:y", "context": context.format(outside=outside)})
    assert result.is_error
    assert "outside the workspace" in text(result)


@pytest.mark.parametrize("dockerfile", ["../outside/Dockerfile", "{outside}/Dockerfile", "link/Dockerfile"])
@pytest.mark.bwrap
async def test_build_dockerfile_must_stay_in_workspace(connect, workspace, outside, dockerfile):
    (workspace / "link").symlink_to(outside)
    async with connect("docker") as client:
        result = await client.call_tool(
            "docker_build", {"tag": "x:y", "dockerfile": dockerfile.format(outside=outside)}
        )
    assert result.is_error
    assert "outside the workspace" in text(result)


async def test_build_validates_tag_and_dockerfile(connect, workspace):
    async with connect("docker") as client:
        bad_tag = await client.call_tool("docker_build", {"tag": "--privileged"})
        no_file = await client.call_tool("docker_build", {"tag": "x:y"})
    assert bad_tag.is_error and "invalid tag" in text(bad_tag)
    assert no_file.is_error and "no Dockerfile" in text(no_file)


@pytest.mark.parametrize("file", ["../outside/compose.yaml", "{outside}/compose.yaml", "link/compose.yaml"])
@pytest.mark.bwrap
async def test_compose_file_must_stay_in_workspace(connect, workspace, outside, file):
    (workspace / "link").symlink_to(outside)
    async with connect("docker") as client:
        result = await client.call_tool("compose", {"action": "ps", "file": file.format(outside=outside)})
    assert result.is_error
    assert "outside the workspace" in text(result)


async def test_compose_without_file_errors(connect):
    async with connect("docker") as client:
        result = await client.call_tool("compose", {"action": "ps"})
    assert result.is_error
    assert "no compose file found" in text(result)


async def test_compose_rejects_unknown_action(connect):
    async with connect("docker") as client:
        result = await client.call_tool("compose", {"action": "exec"})
    assert result.is_error


async def test_run_validates_inputs(connect):
    cases = [
        ({"image": "-v/:/host"}, "invalid image"),
        ({"image": IMAGE, "name": "--privileged"}, "invalid container name"),
        ({"image": IMAGE, "ports": ["80;rm"]}, "invalid port"),
        ({"image": IMAGE, "env": {"BAD-NAME": "1"}}, "invalid environment variable"),
    ]
    async with connect("docker") as client:
        for args, message in cases:
            result = await client.call_tool("docker_run", args)
            assert result.is_error, args
            assert message in text(result)


async def test_container_tools_reject_option_like_names(connect):
    async with connect("docker") as client:
        for tool, args in [
            ("docker_logs", {}),
            ("docker_stop", {}),
            ("docker_rm", {}),
            ("docker_exec", {"command": "id"}),
        ]:
            result = await client.call_tool(tool, {"container": "--help", **args})
            assert result.is_error, tool
            assert "invalid container" in text(result)


async def test_logs_tail_bounds(connect):
    async with connect("docker") as client:
        result = await client.call_tool("docker_logs", {"container": "x", "tail": 0})
    assert result.is_error


# --- against a real daemon -----------------------------------------------------------------


@pytest.mark.docker
async def test_detached_container_lifecycle(connect, workspace, cleanup):
    (workspace / "hello.txt").write_text("from the workspace\n")
    name = f"dev-link-test-{secrets.token_hex(3)}"
    async with connect("docker") as client:
        run = await client.call_tool(
            "docker_run",
            {
                "image": IMAGE,
                "name": name,
                "command": ["sh", "-c", "echo started; echo oops >&2; sleep 300"],
                "ports": ["8080"],
                "env": {"GREETING": "hi there"},
            },
        )
        assert not run.is_error, text(run)
        assert f"({name})" in text(run)
        assert "127.0.0.1:" in text(run)

        inspect = json.loads(sh("inspect", name).stdout)[0]
        assert inspect["Config"]["Labels"]["dev-link.workspace"] == str(workspace.resolve())
        assert inspect["HostConfig"]["Privileged"] is False
        assert inspect["HostConfig"]["NetworkMode"] != "host"
        assert [m["Source"] for m in inspect["Mounts"]] == [str(workspace.resolve())]
        assert inspect["Config"]["WorkingDir"] == "/workspace"

        ps = await client.call_tool("docker_ps", {})
        mine = [c for c in ps.structured_content["containers"] if c["name"] == name]
        assert mine and mine[0]["workspace"] is True and mine[0]["state"] == "running"

        exec_ok = await client.call_tool(
            "docker_exec", {"container": name, "command": "cat hello.txt; echo $GREETING; pwd"}
        )
        assert not exec_ok.is_error
        out = text(exec_ok)
        assert "from the workspace" in out and "hi there" in out and "/workspace" in out

        exec_fail = await client.call_tool("docker_exec", {"container": name, "command": "exit 3"})
        assert not exec_fail.is_error
        assert "exit_code=3" in text(exec_fail)

        writes = await client.call_tool("docker_exec", {"container": name, "command": "echo made > made.txt"})
        assert not writes.is_error
        assert (workspace / "made.txt").read_text() == "made\n"

        logs = await client.call_tool("docker_logs", {"container": name, "tail": 5})
        assert not logs.is_error
        assert "started" in text(logs) and "oops" in text(logs)

        rm_running = await client.call_tool("docker_rm", {"container": name})
        assert rm_running.is_error

        stop = await client.call_tool("docker_stop", {"container": name})
        assert not stop.is_error, text(stop)
        exec_stopped = await client.call_tool("docker_exec", {"container": name, "command": "true"})
        assert exec_stopped.is_error and "not running" in text(exec_stopped)

        all_ps = await client.call_tool("docker_ps", {"all": True})
        assert any(c["name"] == name for c in all_ps.structured_content["containers"])

        rm = await client.call_tool("docker_rm", {"container": name})
        assert not rm.is_error, text(rm)
    assert sh("inspect", name).returncode != 0


@pytest.mark.docker
async def test_run_without_workspace_mount(connect, workspace, cleanup):
    async with connect("docker") as client:
        result = await client.call_tool(
            "docker_run",
            {"image": IMAGE, "command": ["ls", "/workspace"], "detach": False, "mount_workspace": False},
        )
    assert not result.is_error
    assert "exit_code=1" in text(result) or "exit_code=2" in text(result)


@pytest.mark.docker
async def test_foreground_run_reports_exit_code_and_output(connect, workspace, cleanup):
    async with connect("docker") as client:
        result = await client.call_tool(
            "docker_run", {"image": IMAGE, "command": ["sh", "-c", "echo out; echo err >&2; exit 4"], "detach": False}
        )
    assert not result.is_error
    out = text(result)
    assert "exit_code=4" in out and "out" in out and "err" in out
    assert re.search(r"container dev-link-\w+ exited and was removed", out)
    # --rm: a finished foreground run leaves no exited container behind.
    label = f"label=dev-link.workspace={workspace.resolve()}"
    assert sh("ps", "-aq", "--filter", label).stdout.strip() == ""


@pytest.mark.docker
async def test_foreground_run_timeout_removes_container(connect, workspace, cleanup):
    async with connect("docker") as client:
        result = await client.call_tool(
            "docker_run", {"image": IMAGE, "command": ["sleep", "60"], "detach": False, "timeout_s": 3}
        )
    assert not result.is_error
    out = text(result)
    assert "TIMED OUT" in out and "killed and removed" in out
    label = f"label=dev-link.workspace={workspace.resolve()}"
    assert sh("ps", "-aq", "--filter", label).stdout.strip() == ""


@pytest.mark.docker
async def test_run_missing_image_is_error(connect, cleanup):
    async with connect("docker") as client:
        result = await client.call_tool(
            "docker_run", {"image": f"dev-link-missing-{secrets.token_hex(4)}:none", "detach": False}
        )
    assert result.is_error
    assert "docker run" in text(result)


@pytest.mark.docker
async def test_foreign_containers_are_refused(connect, workspace):
    name = f"dev-link-foreign-{secrets.token_hex(3)}"
    assert sh("run", "-d", "--name", name, IMAGE, "sleep", "300").returncode == 0
    try:
        async with connect("docker") as client:
            for tool, args in [
                ("docker_logs", {}),
                ("docker_stop", {}),
                ("docker_rm", {"force": True}),
                ("docker_exec", {"command": "id"}),
            ]:
                result = await client.call_tool(tool, {"container": name, **args})
                assert result.is_error, tool
                assert "not started from this workspace" in text(result)
            ps = await client.call_tool("docker_ps", {})
            mine = [c for c in ps.structured_content["containers"] if c["name"] == name]
            assert mine and mine[0]["workspace"] is False
        assert json.loads(sh("inspect", name).stdout)[0]["State"]["Running"] is True
    finally:
        sh("rm", "-f", name)


@pytest.mark.docker
async def test_unknown_container_is_error(connect):
    async with connect("docker") as client:
        result = await client.call_tool("docker_stop", {"container": f"nope-{secrets.token_hex(4)}"})
    assert result.is_error
    assert "no such container" in text(result)


@pytest.mark.docker
async def test_images_lists_busybox(connect):
    async with connect("docker") as client:
        result = await client.call_tool("docker_images", {})
    assert not result.is_error
    images = result.structured_content["images"]
    assert any(i["repository"] == "busybox" and i["tag"] == "latest" for i in images)


@pytest.mark.docker
async def test_build_labels_image_and_reports_failures(connect, workspace, cleanup):
    (workspace / "app").mkdir()
    (workspace / "app" / "hello.txt").write_text("built\n")
    (workspace / "app" / "Dockerfile").write_text(f"FROM {IMAGE}\nCOPY hello.txt /hello.txt\n")
    (workspace / "Broken.Dockerfile").write_text(f"FROM {IMAGE}\nRUN exit 7\n")
    tag = f"dev-link-test-{secrets.token_hex(4)}:dev"
    async with connect("docker") as client:
        ok = await client.call_tool("docker_build", {"tag": tag, "context": "app"})
        assert not ok.is_error, text(ok)
        assert f"built {tag}" in text(ok)
        labels = json.loads(sh("image", "inspect", "--format", "{{json .Config.Labels}}", tag).stdout)
        assert labels["dev-link.workspace"] == str(workspace.resolve())

        broken = await client.call_tool(
            "docker_build", {"tag": tag + "x", "context": "app", "dockerfile": "Broken.Dockerfile"}
        )
        assert broken.is_error
        assert "failed" in text(broken)

        run = await client.call_tool("docker_run", {"image": tag, "command": ["cat", "/hello.txt"], "detach": False})
        assert "built" in text(run)


@pytest.mark.docker
async def test_compose_up_ps_logs_down(connect, workspace, cleanup):
    project = f"dev-link-test-{secrets.token_hex(3)}"
    (workspace / "stack").mkdir()
    (workspace / "stack" / "compose.yaml").write_text(
        f"name: {project}\n"
        "services:\n"
        "  app:\n"
        f"    image: {IMAGE}\n"
        "    command: [sh, -c, 'echo compose-hello; sleep 300']\n"
        "    volumes: ['./:/data']\n"
    )
    try:
        async with connect("docker") as client:
            up = await client.call_tool("compose", {"action": "up", "file": "stack/compose.yaml"})
            assert not up.is_error, text(up)

            ps = await client.call_tool("compose", {"action": "ps", "file": "stack/compose.yaml"})
            assert not ps.is_error
            assert f"{project}-app-1" in text(ps)

            # Compose containers carry the project dir label, so they count as workspace containers.
            exec_ok = await client.call_tool("docker_exec", {"container": f"{project}-app-1", "command": "ls /data"})
            assert "compose.yaml" in text(exec_ok)

            logs = await client.call_tool(
                "compose", {"action": "logs", "file": "stack/compose.yaml", "services": ["app"], "tail": 10}
            )
            assert not logs.is_error
            assert "compose-hello" in text(logs)

            down = await client.call_tool("compose", {"action": "down", "file": "stack/compose.yaml"})
            assert not down.is_error, text(down)
        assert sh("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}").stdout.strip() == ""
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


@pytest.mark.docker
async def test_shutdown_stops_only_what_this_server_started(connect, workspace, cleanup):
    project = f"dev-link-test-{secrets.token_hex(3)}"
    (workspace / "compose.yaml").write_text(
        f"name: {project}\nservices:\n  app:\n    image: {IMAGE}\n    command: [sleep, '300']\n"
    )
    # Same workspace label, but started by someone else (an earlier server run, say).
    other = f"dev-link-test-{secrets.token_hex(3)}"
    label = f"dev-link.workspace={workspace.resolve()}"
    assert sh("run", "-d", "--label", label, "--name", other, IMAGE, "sleep", "300").returncode == 0
    try:
        async with connect("docker") as client:
            run = await client.call_tool("docker_run", {"image": IMAGE, "command": ["sleep", "300"]})
            assert not run.is_error, text(run)
            cid = text(run).split()[2]
            up = await client.call_tool("compose", {"action": "up"})
            assert not up.is_error, text(up)

        def running(ref: str) -> str:
            return sh("inspect", "--format", "{{.State.Running}}", ref).stdout.strip()

        assert running(cid) == "false"
        assert running(f"{project}-app-1") == "false"
        assert running(other) == "true"
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


@pytest.mark.docker
async def test_compose_action_uses_the_checked_file(connect, workspace, monkeypatch, cleanup):
    project = f"dev-link-test-{secrets.token_hex(3)}"
    compose = workspace / "compose.yaml"
    compose.write_text(f"name: {project}\nservices:\n  checked:\n    image: {IMAGE}\n    command: [sleep, '300']\n")
    real = docker_mod.run_docker

    async def swap_after_check(args, **kwargs):
        result = await real(args, **kwargs)
        if "config" in args:
            # Rewrite the file between the check and the action; the action must not see it.
            compose.write_text(
                f"name: {project}\nservices:\n  swapped:\n    image: {IMAGE}\n    command: [sleep, '300']\n"
            )
        return result

    monkeypatch.setattr(docker_mod, "run_docker", swap_after_check)
    try:
        async with connect("docker") as client:
            up = await client.call_tool("compose", {"action": "up"})
            assert not up.is_error, text(up)
        names = sh("ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Names}}")
        assert names.stdout.split() == [f"{project}-checked-1"]
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


@pytest.mark.docker
async def test_compose_default_file_and_refusal(connect, workspace):
    (workspace / "compose.yaml").write_text(
        f"services:\n  escape:\n    image: {IMAGE}\n    privileged: true\n    volumes: ['/:/host']\n"
    )
    async with connect("docker") as client:
        result = await client.call_tool("compose", {"action": "up"})
    assert result.is_error
    out = text(result)
    assert "compose file refused" in out and "privileged" in out and "'/' is outside" in out


@pytest.mark.docker
async def test_compose_invalid_file_is_error(connect, workspace):
    (workspace / "compose.yaml").write_text("services: [not, a, mapping]\n")
    async with connect("docker") as client:
        result = await client.call_tool("compose", {"action": "build"})
    assert result.is_error
    assert "compose config failed" in text(result)


@pytest.mark.docker
async def test_compose_rejects_bad_service_name(connect, workspace):
    (workspace / "compose.yaml").write_text(f"services:\n  a:\n    image: {IMAGE}\n")
    async with connect("docker") as client:
        result = await client.call_tool("compose", {"action": "ps", "services": ["--all"]})
    assert result.is_error
    assert "invalid service" in text(result)


def test_compose_violations_flags_host_reaching_keys(workspace):
    root = workspace.resolve()
    config = {
        "name": "p",
        "models": {"m": {}},
        "services": {
            "bad": {
                "provider": {"type": str(root / "evil.sh")},
                "use_api_socket": True,
                "runtime": "runc",
                "device_cgroup_rules": ["b *:* rwm"],
                "cgroup": "host",
                "network_mode": "container:other",
                "pid": "container:other",
                "security_opt": ["seccomp=./allow-all.json", "no-new-privileges:true"],
                "post_start": [{"command": ["id"], "privileged": True}],
                "env_file": [{"path": "/etc/environment"}, {"path": str(root / ".env")}],
                "label_file": ["/etc/os-release"],
                "volumes": [{"type": "npipe", "source": "x", "target": "/x"}],
                "build": {
                    "context": str(root),
                    "dockerfile": "../../etc/hostname",
                    "ssh": ["default"],
                    "privileged": True,
                    "entitlements": ["network.host"],
                    "network": "host",
                    "cache_to": ["type=local,dest=/tmp/cache"],
                    "cache_from": [f"type=local,src={root}/cache"],
                },
            }
        },
        "volumes": {
            "ext": {"external": True, "name": "other_db"},
            "plug": {"driver": "local-persist"},
            "rel": {"driver_opts": {"type": "none", "o": "bind", "device": "etc"}},
        },
        "networks": {"hn": {"external": True, "name": "host"}},
    }
    problems = "\n".join(docker_mod.compose_violations(config, root))
    for expected in (
        "top-level key 'models'",
        "provider is not allowed",
        "use_api_socket is not allowed",
        "runtime is not allowed",
        "device_cgroup_rules",
        "cgroup: host",
        "network_mode: container:other",
        "pid: container:other",
        "security_opt 'seccomp=./allow-all.json'",
        "privileged lifecycle hooks",
        "env_file '/etc/environment'",
        "label_file '/etc/os-release'",
        "npipe mounts",
        "build dockerfile",
        "build.ssh",
        "build.privileged",
        "build.entitlements",
        "build network host",
        "'/tmp/cache'",
        "volume 'ext': external",
        "driver 'local-persist'",
        "device 'etc'",
        "network 'hn'",
    ):
        assert expected in problems, expected
    assert "no-new-privileges" not in problems
    assert ".env" not in problems
    assert f"{root}/cache" not in problems


@pytest.mark.parametrize("kind", ["nfs", "nfs4", "cifs", "overlay", "ext4"])
def test_compose_violations_refuses_volume_mount_types(workspace, kind):
    # The daemon mounts these as root on the host: network shares, or host paths named in `o`.
    root = workspace.resolve()
    opts = {"type": kind, "o": "addr=127.0.0.1,ro", "device": str(root / "data")}
    problems = docker_mod.compose_violations({"volumes": {"share": {"driver_opts": opts}}}, root)
    assert problems == [f"volume 'share': driver_opts type {kind!r} is not allowed (only none, bind, tmpfs)"]


@pytest.mark.parametrize("opts", [{"type": "none", "o": "bind"}, {"type": "bind"}, {"type": "tmpfs"}, {"o": "bind"}])
def test_compose_violations_accepts_local_volume_types(workspace, opts):
    root = workspace.resolve()
    volume = {"driver_opts": {**opts, "device": str(root / "data")}}
    assert docker_mod.compose_violations({"volumes": {"v": volume}}, root) == []


@pytest.mark.docker
async def test_compose_typical_file_passes_check(connect, workspace):
    """A realistic dev stack must not trip the allowlist after compose normalizes it."""
    (workspace / "db.secret").write_text("pw\n")
    (workspace / ".env").write_text("APP_PORT=8081\n")
    (workspace / "app.env").write_text("MODE=dev\n")
    (workspace / "Dockerfile").write_text(f"FROM {IMAGE}\n")
    (workspace / "compose.yaml").write_text(
        f"""name: dev-link-test-{secrets.token_hex(3)}
services:
  db:
    image: {IMAGE}
    command: [sleep, "300"]
    environment: {{POSTGRES_PASSWORD_FILE: /run/secrets/db}}
    secrets: [db]
    volumes: [data:/var/lib/data, ./:/src:ro]
    healthcheck: {{test: [CMD, "true"], interval: 5s}}
    restart: unless-stopped
    ulimits: {{nofile: 1024}}
    tmpfs: [/scratch]
    labels: {{team: dev}}
    logging: {{driver: json-file}}
    deploy: {{resources: {{limits: {{memory: 256m}}}}}}
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
  app:
    build: {{context: ., args: {{X: "1"}}}}
    env_file: [app.env]
    ports: ["127.0.0.1:${{APP_PORT}}:80"]
    depends_on: {{db: {{condition: service_healthy}}}}
    networks: [back]
    user: "1000"
    working_dir: /src
    init: true
    extra_hosts: ["api.local:127.0.0.1"]
    stop_grace_period: 3s
volumes:
  data: {{}}
networks:
  back: {{}}
secrets:
  db: {{file: ./db.secret}}
"""
    )
    async with connect("docker") as client:
        result = await client.call_tool("compose", {"action": "ps"})
    assert not result.is_error, text(result)


@pytest.mark.docker
async def test_compose_provider_service_is_refused_on_every_action(connect, workspace, tmp_path):
    marker = tmp_path / "ran"
    script = workspace / "evil.sh"
    script.write_text(f'#!/bin/sh\necho "$*" >> {marker}\n')
    script.chmod(0o755)
    (workspace / "compose.yaml").write_text(f"services:\n  p:\n    provider:\n      type: {script}\n")
    async with connect("docker") as client:
        for action in ("up", "down", "ps"):
            result = await client.call_tool("compose", {"action": action})
            assert result.is_error, action
            assert "provider is not allowed" in text(result)
    assert not marker.exists()


@pytest.mark.docker
async def test_compose_env_file_outside_workspace_is_refused(connect, workspace, outside):
    (outside / "secret.env").write_text("SECRET=topsecret\n")
    (workspace / "compose.yaml").write_text(
        f"services:\n  a:\n    image: {IMAGE}\n    env_file: [{outside}/secret.env]\n"
    )
    async with connect("docker") as client:
        result = await client.call_tool("compose", {"action": "up"})
    assert result.is_error
    assert "env_file" in text(result) and "outside the workspace" in text(result)
    assert "topsecret" not in text(result)


@pytest.mark.docker
async def test_compose_cannot_take_over_foreign_project(connect, workspace, outside):
    project = f"dev-link-foreign-{secrets.token_hex(3)}"
    (outside / "compose.yaml").write_text(
        f"name: {project}\nservices:\n  v:\n    image: {IMAGE}\n    command: sleep 300\n"
    )
    assert sh("compose", "-f", str(outside / "compose.yaml"), "up", "-d").returncode == 0
    try:
        (workspace / "compose.yaml").write_text(f"name: {project}\nservices:\n  v:\n    image: {IMAGE}\n")
        async with connect("docker") as client:
            for action in ("down", "logs", "up"):
                result = await client.call_tool("compose", {"action": action})
                assert result.is_error, action
                assert "from outside this workspace" in text(result)
        running = sh("ps", "-q", "--filter", f"label=com.docker.compose.project={project}").stdout.strip()
        assert running
    finally:
        sh("compose", "-f", str(outside / "compose.yaml"), "down")


@pytest.mark.docker
async def test_compose_action_uses_the_checked_env_file(connect, workspace, monkeypatch, cleanup):
    project = f"dev-link-test-{secrets.token_hex(3)}"
    (workspace / ".env").write_text("V=checked\n")
    (workspace / "compose.yaml").write_text(
        f"name: {project}\nservices:\n  app:\n    image: {IMAGE}\n    command: [sleep, '300']\n"
        "    environment:\n      V: ${V}\n"
    )
    real = docker_mod.run_docker

    async def swap_after_check(args, **kwargs):
        result = await real(args, **kwargs)
        if "config" in args:
            # .env is read by path, so the action must run the resolved config, not re-read it.
            (workspace / ".env").write_text("V=swapped\n")
        return result

    monkeypatch.setattr(docker_mod, "run_docker", swap_after_check)
    try:
        async with connect("docker") as client:
            up = await client.call_tool("compose", {"action": "up"})
            assert not up.is_error, text(up)
        env = sh("inspect", f"{project}-app-1", "--format", "{{json .Config.Env}}")
        assert "V=checked" in env.stdout
        assert "V=swapped" not in env.stdout
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


# --- with the network off ------------------------------------------------------------------


def test_offline_compose_makes_networks_internal_and_stops_pulls():
    config = {
        "name": "p",
        "services": {
            "web": {"image": "nginx", "networks": {"default": None, "back": None}},
            "db": {"image": "postgres", "networks": {"back": None}, "pull_policy": "missing"},
            "side": {"image": "busybox", "network_mode": "service:web"},
            "solo": {"image": "busybox", "network_mode": "none"},
        },
        "networks": {"default": {"name": "p_default"}, "back": {"name": "p_back", "driver": "bridge"}},
    }
    rewritten, problems = docker_mod.offline_compose(config)
    assert problems == []
    isolated = {
        "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
        "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
    }
    for net in rewritten["networks"].values():
        assert net["internal"] is True
        assert net["driver_opts"] == isolated
    assert rewritten["services"]["web"]["pull_policy"] == "never"
    assert rewritten["services"]["db"]["pull_policy"] == "never"
    assert "internal" not in config["networks"]["default"]


def test_offline_compose_refuses_what_reaches_the_network():
    config = {
        "services": {
            "pub": {"image": "nginx", "ports": [{"target": 80, "published": "8080"}]},
            "bridge": {"image": "busybox", "network_mode": "bridge"},
            "orphan": {"image": "busybox", "network_mode": "service:nope"},
            "always": {"image": "busybox", "pull_policy": "always"},
            "gelf": {"image": "busybox", "logging": {"driver": "gelf"}},
            "b": {"image": "b", "build": {"context": "/ws"}},
        },
        "networks": {
            "ext": {"name": "ext", "external": True},
            "mac": {"name": "mac", "driver": "macvlan"},
            "opts": {"name": "opts", "driver_opts": {"com.docker.network.bridge.name": "docker0"}},
        },
    }
    _, problems = docker_mod.offline_compose(config)
    joined = "\n".join(problems)
    for expected in (
        "'pub': ports",
        "network_mode: bridge",
        "network_mode: service:nope",
        "pull_policy always",
        "logging driver gelf",
        "'b' has build: the server's network is off, so images cannot be built",
        "network 'ext': external",
        "network 'mac': driver macvlan",
        "network 'opts': driver_opts",
    ):
        assert expected in joined, expected


async def test_run_refuses_ports_with_network_off(connect, rt):
    rt.config.sandbox.network = False
    async with connect("docker") as client:
        result = await client.call_tool("docker_run", {"image": IMAGE, "ports": ["8080:80"]})
    assert result.is_error
    assert "network is off" in text(result)


def _probe(target: str) -> str:
    # Prints which of the outside world the container can reach; busybox has wget and nslookup.
    return (
        f"wget -T 3 -q -O /dev/null http://{target}/ && echo REACHED || echo NO-REACH;"
        " nslookup -timeout=2 example.com >/dev/null 2>&1 && echo RESOLVED || echo NO-DNS"
    )


@pytest.mark.docker
async def test_run_with_network_off_has_no_network(connect, rt, cleanup):
    rt.config.sandbox.network = False
    async with connect("docker") as client:
        run = await client.call_tool(
            "docker_run", {"image": IMAGE, "command": ["sh", "-c", _probe("1.1.1.1")], "detach": False}
        )
        assert not run.is_error, text(run)
        assert "NO-REACH" in text(run) and "NO-DNS" in text(run)
        detached = await client.call_tool("docker_run", {"image": IMAGE, "command": ["sleep", "300"]})
        assert not detached.is_error, text(detached)
        cid = text(detached).split()[2]
        assert sh("inspect", "--format", "{{.HostConfig.NetworkMode}}", cid).stdout.strip() == "none"
        missing = await client.call_tool("docker_run", {"image": "dev-link-missing:none"})
    assert missing.is_error
    assert "must already exist locally" in text(missing)


async def test_build_is_refused_with_network_off(connect, workspace, rt):
    rt.config.sandbox.network = False
    (workspace / "Dockerfile").write_text(f"FROM {IMAGE}\n")
    async with connect("docker") as client:
        result = await client.call_tool("docker_build", {"tag": "x:y"})
    assert result.is_error
    assert "images cannot be built" in text(result)


@pytest.mark.docker
async def test_compose_with_network_off(connect, workspace, rt, cleanup):
    rt.config.sandbox.network = False
    project = f"dev-link-test-{secrets.token_hex(3)}"
    (workspace / "compose.yaml").write_text(
        f"name: {project}\n"
        "services:\n"
        "  web:\n"
        f"    image: {IMAGE}\n"
        "    command: [sh, -c, 'echo hello > /index.html; httpd -f -p 8080 -h /']\n"
        "  probe:\n"
        f"    image: {IMAGE}\n"
        "    command: [sleep, '300']\n"
    )
    try:
        async with connect("docker") as client:
            up = await client.call_tool("compose", {"action": "up"})
            assert not up.is_error, text(up)
            inside = await client.call_tool(
                "docker_exec",
                {"container": f"{project}-probe-1", "command": "wget -T 3 -q -O- http://web:8080/"},
            )
            assert "hello" in text(inside)
            outside = await client.call_tool(
                "docker_exec", {"container": f"{project}-probe-1", "command": _probe("1.1.1.1")}
            )
            assert "NO-REACH" in text(outside) and "NO-DNS" in text(outside)
            # Restarting a project is fine while it stays on its internal networks.
            restart = await client.call_tool("compose", {"action": "restart"})
            assert not restart.is_error, text(restart)
            (workspace / "compose.yaml").write_text(
                f"name: {project}\nservices:\n  web:\n    image: {IMAGE}\n    ports: ['8080']\n"
            )
            ports = await client.call_tool("compose", {"action": "up"})
            assert ports.is_error and "ports cannot be published" in text(ports)
            (workspace / "Dockerfile").write_text(f"FROM {IMAGE}\n")
            (workspace / "compose.yaml").write_text(f"name: {project}\nservices:\n  app:\n    build: .\n")
            for action in ("up", "restart", "build"):
                built = await client.call_tool("compose", {"action": action})
                assert built.is_error and "images cannot be built" in text(built), action
            down = await client.call_tool("compose", {"action": "down"})
            assert not down.is_error, text(down)
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


@pytest.mark.docker
async def test_network_off_refuses_containers_started_with_network(connect, workspace, rt, cleanup):
    project = f"dev-link-test-{secrets.token_hex(3)}"
    (workspace / "compose.yaml").write_text(
        f"name: {project}\nservices:\n  app:\n    image: {IMAGE}\n    command: [sleep, '300']\n"
    )
    other = f"dev-link-test-{secrets.token_hex(3)}"
    label = f"dev-link.workspace={workspace.resolve()}"
    # Started from this workspace by an earlier server that had the network on.
    assert sh("run", "-d", "--label", label, "--name", other, IMAGE, "sleep", "300").returncode == 0
    assert sh("compose", "-f", str(workspace / "compose.yaml"), "up", "-d").returncode == 0
    try:
        rt.config.sandbox.network = False
        async with connect("docker") as client:
            exec_run = await client.call_tool("docker_exec", {"container": other, "command": "true"})
            assert exec_run.is_error and "has network access" in text(exec_run)
            for action in ("restart", "up"):
                result = await client.call_tool("compose", {"action": action})
                assert result.is_error, action
                assert "compose down first" in text(result)
            down = await client.call_tool("compose", {"action": "down"})
            assert not down.is_error, text(down)
            up = await client.call_tool("compose", {"action": "up"})
            assert not up.is_error, text(up)
            exec_ok = await client.call_tool("docker_exec", {"container": f"{project}-app-1", "command": "true"})
            assert not exec_ok.is_error, text(exec_ok)
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


def _compose_file(workspace: Path, project: str, extra: str = "") -> None:
    """Write a compose file with a sleeping `probe` service; `extra` goes at the end, so indented lines
    extend that service."""
    (workspace / "compose.yaml").write_text(
        f"name: {project}\nservices:\n  probe:\n    image: {IMAGE}\n    command: [sleep, '300']\n{extra}"
    )


@pytest.mark.docker
async def test_compose_with_network_off_cannot_reach_the_host(connect, workspace, rt, cleanup):
    rt.config.sandbox.network = False
    project = f"dev-link-test-{secrets.token_hex(3)}"
    _compose_file(workspace, project)
    try:
        async with connect("docker") as client:
            up = await client.call_tool("compose", {"action": "up"})
            assert not up.is_error, text(up)
            subnet = sh("network", "inspect", "--format", "{{(index .IPAM.Config 0).Subnet}}", f"{project}_default")
            gateway = next(ipaddress.ip_network(subnet.stdout.strip()).hosts())
            # A host service listening on every address, like a dev database on *:3306.
            with socket.create_server(("0.0.0.0", 0)) as server:
                port = server.getsockname()[1]
                probe = await client.call_tool(
                    "docker_exec",
                    {
                        "container": f"{project}-probe-1",
                        "command": f"nc -w 3 {gateway} {port} </dev/null && echo REACHED || echo NO-REACH",
                    },
                )
        assert "NO-REACH" in text(probe), text(probe)
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


@pytest.mark.docker
async def test_compose_with_network_off_needs_local_image_volumes(connect, workspace, rt, cleanup):
    rt.config.sandbox.network = False
    project = f"dev-link-test-{secrets.token_hex(3)}"
    mount = "    volumes:\n      - type: image\n        source: {}\n        target: /img\n"
    try:
        async with connect("docker") as client:
            # The daemon pulls image volume sources even with pull_policy never.
            _compose_file(workspace, project, mount.format("dev-link-missing:none"))
            missing = await client.call_tool("compose", {"action": "up"})
            assert missing.is_error
            assert "image volume dev-link-missing:none is not available locally" in text(missing)
            _compose_file(workspace, project, mount.format(IMAGE))
            up = await client.call_tool("compose", {"action": "up"})
            assert not up.is_error, text(up)
            listed = await client.call_tool(
                "docker_exec", {"container": f"{project}-probe-1", "command": "test -x /img/bin/sh && echo MOUNTED"}
            )
            assert "MOUNTED" in text(listed), text(listed)
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


@pytest.mark.docker
async def test_network_off_refuses_internal_networks_without_isolation(connect, workspace, rt, cleanup):
    project = f"dev-link-test-{secrets.token_hex(3)}"
    # Internal but created without isolated gateways: the host still has an address on it.
    _compose_file(workspace, project, "networks:\n  default:\n    internal: true\n")
    assert sh("compose", "-f", str(workspace / "compose.yaml"), "up", "-d").returncode == 0
    try:
        rt.config.sandbox.network = False
        async with connect("docker") as client:
            for action in ("up", "restart"):
                result = await client.call_tool("compose", {"action": action})
                assert result.is_error, action
                assert f"network {project}_default of project" in text(result)
                assert "compose down first" in text(result)
            exec_run = await client.call_tool("docker_exec", {"container": f"{project}-probe-1", "command": "true"})
            assert exec_run.is_error and "has network access" in text(exec_run)
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


def _fake_docker(monkeypatch, prefix: list[str], fake: RunResult) -> None:
    """Answer docker commands starting with `prefix` with `fake`; the rest run for real."""
    real = docker_mod.run_docker

    async def run(args, **kwargs):
        if list(args[: len(prefix)]) == prefix:
            return fake
        return await real(args, **kwargs)

    monkeypatch.setattr(docker_mod, "run_docker", run)


@pytest.mark.docker
async def test_network_off_compose_needs_docker_28(connect, workspace, rt, monkeypatch, cleanup):
    rt.config.sandbox.network = False
    project = f"dev-link-test-{secrets.token_hex(3)}"
    _compose_file(workspace, project)
    _fake_docker(monkeypatch, ["version"], RunResult(0, "27.5.1\n", "", 0.1, False, False))
    try:
        async with connect("docker") as client:
            result = await client.call_tool("compose", {"action": "up"})
        assert result.is_error
        assert "Docker 27.5.1 cannot isolate internal networks from the host; it needs Docker 28" in text(result)
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")


@pytest.mark.docker
@pytest.mark.parametrize(
    "fake",
    [
        RunResult(1, "", "Error response from daemon: permission denied\n", 0.1, False, False),
        RunResult(None, "", "", 30.0, True, False),
    ],
)
async def test_network_off_compose_fails_closed_when_networks_cannot_be_inspected(
    connect, workspace, rt, monkeypatch, cleanup, fake
):
    rt.config.sandbox.network = False
    project = f"dev-link-test-{secrets.token_hex(3)}"
    _compose_file(workspace, project)
    _fake_docker(monkeypatch, ["network", "inspect"], fake)
    try:
        async with connect("docker") as client:
            result = await client.call_tool("compose", {"action": "up"})
        assert result.is_error
        assert "docker network inspect failed" in text(result)
        assert sh("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}").stdout.strip() == ""
    finally:
        sh("compose", "-p", project, "down", "--remove-orphans")
