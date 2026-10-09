import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from dev_link_mcp.config import Config
from dev_link_mcp.sandbox import Capture, RunResult, Sandbox


@pytest.fixture
def sandbox(config: Config) -> Sandbox:
    return Sandbox(config)


async def sh(sb: Sandbox, command: str, **kwargs) -> RunResult:
    return await sb.run(sb.shell_argv(command), **kwargs)


# Leftover processes are found by scanning /proc, which only Linux has.
needs_proc = pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to find leftover processes")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); treat it as dead.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


async def test_workspace_is_writable_and_cwd_is_workspace(sandbox, workspace):
    r = await sh(sandbox, "pwd; echo hi > made.txt")
    assert r.ok, r.format()
    assert r.stdout.strip() == str(workspace)
    assert (workspace / "made.txt").read_text() == "hi\n"


async def test_cwd_subdirectory(sandbox, workspace):
    (workspace / "sub").mkdir()
    r = await sh(sandbox, "pwd", cwd=workspace / "sub")
    assert r.stdout.strip() == str(workspace / "sub")


@pytest.mark.bwrap
async def test_files_outside_workspace_are_invisible(sandbox, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("top secret")
    r = await sh(sandbox, f"cat {outside}")
    assert not r.ok
    assert "top secret" not in r.stdout


@pytest.mark.bwrap
async def test_host_home_is_replaced_by_private_home(sandbox, config):
    home = Path.home()
    visible_roots = {Path(p).expanduser() for p in config.sandbox.ro_paths}
    host_entries = [
        p.name
        for p in home.iterdir()
        if not any(p == v or v.is_relative_to(p) for v in visible_roots) and not config.workspace.is_relative_to(p)
    ][:20]
    if not host_entries:
        pytest.skip("host home has no entries to compare")
    r = await sh(sandbox, 'echo "HOME=$HOME"; ls -A "$HOME"')
    assert r.ok, r.format()
    assert f"HOME={home}" in r.stdout
    listed = set(r.stdout.splitlines()[1:])
    assert ".gitconfig" in listed
    assert not listed & set(host_entries), listed & set(host_entries)
    # The private home persists between calls.
    await sh(sandbox, 'echo kept > "$HOME/.dev-link-persist"')
    assert (await sh(sandbox, 'cat "$HOME/.dev-link-persist"')).stdout == "kept\n"
    assert not (home / ".dev-link-persist").exists()


async def test_system_dirs_are_read_only(sandbox):
    r = await sh(sandbox, "touch /etc/dev-link-test 2>&1; echo rc=$?; touch /usr/dev-link-test 2>&1; echo rc=$?")
    assert r.stdout.count("rc=1") == 2, r.format()
    assert not Path("/etc/dev-link-test").exists()


@pytest.mark.bwrap
async def test_tmp_is_private_and_shared_between_calls(sandbox, config):
    host_marker = Path("/tmp") / f"dev-link-host-marker-{os.getpid()}"
    host_marker.write_text("host")
    try:
        r = await sh(sandbox, f"test -e {host_marker} && echo visible || echo hidden; echo x > /tmp/shared")
        assert r.stdout.strip() == "hidden"
        assert (await sh(sandbox, "cat /tmp/shared")).stdout == "x\n"
        assert (config.state_dir / "tmp" / "shared").exists()
    finally:
        host_marker.unlink()


async def test_tmp_is_emptied_at_start(config):
    Sandbox(config)
    (config.state_dir / "tmp" / "stale").write_text("x")
    Sandbox(config)
    assert not (config.state_dir / "tmp" / "stale").exists()


@pytest.mark.bwrap
async def test_server_secrets_not_in_env(sandbox, monkeypatch):
    monkeypatch.setenv("DEV_LINK_FAKE_SECRET", "s3cr3t-value")
    monkeypatch.setenv("TZ", "Australia/Sydney")
    r = await sh(sandbox, "env")
    assert "s3cr3t-value" not in r.stdout
    assert "DEV_LINK_FAKE_SECRET" not in r.stdout
    assert "TZ=Australia/Sydney" in r.stdout  # allowlisted
    assert "NO_COLOR=1" in r.stdout


async def test_env_config_and_extra(config):
    config.sandbox.env = {"FROM_CONFIG": "c", "OVERRIDDEN": "config"}
    sb = Sandbox(config)
    r = await sh(sb, 'echo "$FROM_CONFIG $OVERRIDDEN $EXTRA"', env={"EXTRA": "e", "OVERRIDDEN": "extra"})
    assert r.stdout.strip() == "c extra e"


@pytest.mark.bwrap
def test_extra_env_never_reaches_the_bwrap_process(sandbox, workspace):
    # bwrap is not setuid, so LD_PRELOAD in its own environment would load agent code on the host.
    argv, proc_env = sandbox._launch(["true"], workspace, {"LD_PRELOAD": "/x/evil.so", "MY_VAR": "1"})
    assert "LD_PRELOAD" not in proc_env
    assert "MY_VAR" not in proc_env
    assert argv[argv.index("LD_PRELOAD") - 1] == "--setenv"


@pytest.mark.parametrize("env", [{"1BAD": "x"}, {"A=B": "x"}, {"": "x"}, {"OK": "a\0b"}])
async def test_invalid_extra_env_is_rejected(sandbox, env):
    with pytest.raises(ToolError):
        await sh(sandbox, "true", env=env)


@pytest.mark.bwrap
async def test_spawn_env_does_not_reach_host_bwrap(sandbox, workspace, tmp_path):
    if not shutil.which("gcc"):
        pytest.skip("gcc not available")
    marker = tmp_path / "host_marker"
    (workspace / "evil.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void init(void) {"
        f' FILE *f = fopen("{marker}", "a"); if (f) {{ fputs("x", f); fclose(f); }} }}\n'
    )
    r = await sh(sandbox, "gcc -shared -fPIC -o evil.so evil.c")
    assert r.ok, r.format()
    log_path = tmp_path / "spawn.log"
    with log_path.open("ab") as log_file:
        proc = await sandbox.spawn(
            sandbox.shell_argv('echo "v=$MY_VAR"'),
            cwd=workspace,
            log_file=log_file,
            env={"LD_PRELOAD": str(workspace / "evil.so"), "MY_VAR": "42"},
        )
    await proc.wait()
    assert "v=42" in log_path.read_text()
    assert not marker.exists()


async def test_exit_code_and_stderr(sandbox):
    r = await sh(sandbox, "echo out; echo err >&2; exit 3")
    assert r.exit_code == 3 and not r.ok and not r.timed_out
    assert r.stdout == "out\n" and r.stderr == "err\n"
    text = r.format()
    assert "exit_code=3" in text and "--- stdout ---\nout" in text and "--- stderr ---\nerr" in text


async def test_no_output_format(sandbox):
    r = await sh(sandbox, "true")
    assert r.ok and "(no output)" in r.format()


async def test_stdin(sandbox):
    r = await sandbox.run(["cat"], stdin="line1\nline2\n")
    assert r.stdout == "line1\nline2\n"
    r = await sandbox.run(["wc", "-c"], stdin="")
    assert r.stdout.strip() == "0"


async def test_stdin_larger_than_pipe_with_large_output(sandbox):
    # The command writes a lot before reading stdin; both pipes must be drained concurrently.
    data = "x" * 2_000_000
    r = await sh(sandbox, "head -c 1000000 /dev/zero; wc -c", stdin=data, timeout_s=20)
    assert not r.timed_out, r.format()[:200]
    assert r.stdout.strip().endswith("2000000")


async def test_stdin_not_read_still_times_out(sandbox):
    started = time.monotonic()
    r = await sh(sandbox, "sleep 3050", stdin="y" * 5_000_000, timeout_s=1)
    assert r.timed_out
    assert time.monotonic() - started < 10


@needs_proc
async def test_timeout_kills_tree_including_background_grandchild(sandbox):
    started = time.monotonic()
    r = await sh(sandbox, "bash -c 'sleep 3011 &'; sleep 3012 & echo started; sleep 3010", timeout_s=1.5)
    assert r.timed_out
    assert "started" in r.stdout
    assert time.monotonic() - started < 10
    assert "TIMED OUT" in r.format()
    await asyncio.sleep(0.3)
    assert not _sleepers_alive(("3010", "3011", "3012"))


def _sleepers_alive(args: tuple[str, ...]) -> list[int]:
    """Host pids of `sleep <arg>` processes owned by this user."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(cmd) >= 2 and cmd[0].endswith(b"sleep") and cmd[1].decode() in args and _pid_alive(int(entry.name)):
            found.append(int(entry.name))
    return found


@pytest.mark.bwrap
async def test_timeout_kills_detached_grandchild_holding_no_pipes(sandbox):
    # setsid + closed fds: escapes the process group, only the PID namespace catches it.
    r = await sh(sandbox, "setsid sleep 3022 </dev/null >/dev/null 2>&1 & sleep 3020", timeout_s=1)
    assert r.timed_out
    await asyncio.sleep(0.3)
    assert not _sleepers_alive(("3020", "3022"))


@pytest.mark.bwrap
async def test_background_job_dies_when_command_returns(sandbox):
    r = await sh(sandbox, "setsid sleep 3030 </dev/null >/dev/null 2>&1 & echo done")
    assert r.ok and r.stdout == "done\n"
    await asyncio.sleep(0.3)
    assert not _sleepers_alive(("3030",))


@needs_proc
async def test_cancellation_kills_process(sandbox):
    task = asyncio.create_task(sh(sandbox, "sleep 3040", timeout_s=60))
    await asyncio.sleep(0.5)
    assert _sleepers_alive(("3040",))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert not _sleepers_alive(("3040",))


async def test_timeout_is_clamped(config):
    config.limits.default_timeout_s = 7
    config.limits.max_timeout_s = 50
    sb = Sandbox(config)
    assert sb.clamp_timeout(None) == 7
    assert sb.clamp_timeout(0) == 7
    assert sb.clamp_timeout(-1) == 7
    assert sb.clamp_timeout(10) == 10
    assert sb.clamp_timeout(10_000) == 50


async def test_output_truncation_keeps_head_and_tail(sandbox):
    r = await sh(
        sandbox,
        "echo HEAD-MARKER; for i in $(seq 1 20000); do echo middle-line-$i; done; echo TAIL-MARKER",
        max_output_bytes=8192,
    )
    assert r.ok and r.truncated
    assert r.stdout.startswith("HEAD-MARKER")
    assert r.stdout.rstrip().endswith("TAIL-MARKER")
    assert "bytes elided" in r.stdout
    assert len(r.stdout) < 9000
    assert "middle-line-10000\n" not in r.stdout
    assert "(output truncated" in r.format()


def test_capture_unit():
    cap = Capture(4096)
    cap.feed(b"a" * 100)
    assert cap.text() == "a" * 100 and not cap.dropped
    cap = Capture(4096)
    for _ in range(10):
        cap.feed(b"h" * 1000)
    cap.feed(b"END")
    assert cap.dropped == 10_003 - 4096
    out = cap.text()
    assert out.startswith("h" * 2048) and out.endswith("END") and "elided" in out


@pytest.mark.bwrap
async def test_nonexistent_program_inside_sandbox(sandbox):
    r = await sandbox.run(["definitely-not-a-program-xyz"])
    assert not r.ok
    assert "definitely-not-a-program-xyz" in r.stderr


async def test_nonexistent_program_unsandboxed_raises_tool_error(config):
    config.sandbox.backend = "none"
    sb = Sandbox(config)
    with pytest.raises(ToolError, match="cannot start"):
        await sb.run(["definitely-not-a-program-xyz"])


async def test_unsandboxed_run_works(config, workspace):
    config.sandbox.backend = "none"
    sb = Sandbox(config)
    r = await sh(sb, "pwd; echo $HOME")
    # Unsandboxed commands get the host environment, so HOME is the real one.
    assert r.stdout.splitlines() == [str(workspace), os.environ["HOME"]]


def test_missing_bwrap_is_a_clear_error(config, monkeypatch):
    monkeypatch.setattr("dev_link_mcp.sandbox.shutil.which", lambda _: None)
    # The fixture falls back to `none` where bwrap is missing; the refusal comes from `auto`.
    config.sandbox.backend = "auto"
    with pytest.raises(RuntimeError, match="bubblewrap"):
        Sandbox(config)


_CONNECT = "timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' && echo CONNECTED || echo BLOCKED"


@pytest.mark.network
async def test_network_allowed_by_default(sandbox):
    r = await sh(sandbox, _CONNECT, timeout_s=15)
    assert "CONNECTED" in r.stdout, r.format()


@pytest.mark.bwrap
@pytest.mark.network
async def test_no_network_blocks_outbound(config):
    config.sandbox.network = False
    sb = Sandbox(config)
    r = await sh(sb, _CONNECT, timeout_s=15)
    assert "BLOCKED" in r.stdout, r.format()
    # Loopback still works inside the private network namespace.
    r = await sh(sb, "ip link 2>/dev/null || cat /proc/net/dev", timeout_s=15)
    assert "lo" in r.stdout


async def test_git_identity_config_in_private_home(sandbox):
    gitconfig = sandbox.home / ".gitconfig"
    text = gitconfig.read_text()
    assert "directory = *" in text and "defaultBranch = main" in text
    assert "credential" not in text


@pytest.mark.bwrap
async def test_git_commit_works_without_host_identity(sandbox):
    # The host identity is copied when set; otherwise a fallback keeps commits possible.
    r = await sh(sandbox, "git init -q r && cd r && git commit -q --allow-empty -m first && git log -1 --format=%an")
    assert r.ok, r.format()
    assert r.stdout.strip()


@pytest.mark.parametrize("backend", [pytest.param("auto", marks=pytest.mark.bwrap), "none"])
async def test_git_never_waits_for_an_editor(config, workspace, backend):
    config.sandbox.backend = backend
    sb = Sandbox(config)
    (workspace / "a.txt").write_text("a")
    # The repo's own identity, because with the sandbox off git reads the host's config.
    setup = "git init -q && git config user.name T && git config user.email t@example.com && git add a.txt"
    r = await sh(sb, f"{setup} && git commit", timeout_s=60)
    assert not r.timed_out and r.duration_s < 20, r.format()
    assert r.exit_code == 1 and "empty commit message" in r.stderr, r.format()


@pytest.mark.parametrize("backend", [pytest.param("auto", marks=pytest.mark.bwrap), "none"])
async def test_git_does_not_find_a_repository_above_the_workspace(config, workspace, tmp_path, backend):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (workspace / "sub").mkdir()
    config.sandbox.backend = backend
    sb = Sandbox(config)
    r = await sh(sb, "git rev-parse --show-toplevel", cwd=workspace / "sub")
    assert not r.ok and "not a git repository" in r.stderr, r.format()
    # A repository at the workspace root is still found from below it.
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    r = await sh(sb, "git rev-parse --show-toplevel", cwd=workspace / "sub")
    assert r.ok and r.stdout.strip() == str(workspace), r.format()


@pytest.mark.parametrize("backend", [pytest.param("auto", marks=pytest.mark.bwrap), "none"])
async def test_git_env_defaults_yield_to_config(config, workspace, backend):
    config.sandbox.backend = backend
    config.sandbox.env = {"GIT_EDITOR": "vi"}
    sb = Sandbox(config)
    r = await sh(sb, 'echo "$GIT_EDITOR|$GIT_MERGE_AUTOEDIT|$GIT_CEILING_DIRECTORIES"')
    assert r.stdout.strip() == f"vi|no|{workspace.parent}"


async def test_planted_gitconfig_symlink_is_not_followed_on_restart(sandbox, config, tmp_path):
    # The agent owns the private home; recreating .gitconfig at the next start must not write
    # through a symlink it left there.
    target = tmp_path / "host-file-outside"
    r = await sh(sandbox, f'rm "$HOME/.gitconfig" && ln -s {target} "$HOME/.gitconfig"')
    assert r.ok, r.format()
    Sandbox(config)
    assert not target.exists()
