import shutil
import time

import pytest
from conftest import text


async def call(connect, tool, args):
    async with connect("shell") as client:
        return await client.call_tool(tool, args)


def sandbox_has(rt, program):
    return shutil.which(program, path=rt.sandbox.path) is not None


# run_command


async def test_run_command_returns_stdout_and_exit_code(connect):
    r = await call(connect, "run_command", {"command": "echo hello && echo oops >&2"})
    assert not r.is_error
    out = text(r)
    assert out.startswith("exit_code=0")
    assert "--- stdout ---\nhello" in out
    assert "--- stderr ---\noops" in out


async def test_run_command_nonzero_exit_is_not_tool_error(connect):
    r = await call(connect, "run_command", {"command": "exit 3"})
    assert not r.is_error
    assert text(r).startswith("exit_code=3")
    assert "(no output)" in text(r)


async def test_run_command_default_cwd_is_workspace_root(connect, workspace):
    r = await call(connect, "run_command", {"command": "pwd"})
    assert str(workspace) in text(r)


async def test_run_command_cwd_subdirectory(connect, workspace):
    (workspace / "sub").mkdir()
    r = await call(connect, "run_command", {"command": "pwd", "cwd": "sub"})
    assert not r.is_error
    assert str(workspace / "sub") in text(r)


async def test_run_command_writes_into_workspace(connect, workspace):
    r = await call(connect, "run_command", {"command": "echo data > made.txt"})
    assert not r.is_error
    assert (workspace / "made.txt").read_text() == "data\n"


@pytest.mark.parametrize("cwd", ["..", "../..", "/etc", "sub/../../"])
@pytest.mark.bwrap
async def test_run_command_cwd_escape_rejected(connect, cwd):
    r = await call(connect, "run_command", {"command": "pwd", "cwd": cwd})
    assert r.is_error
    assert "outside the workspace" in text(r)


@pytest.mark.bwrap
async def test_run_command_cwd_symlink_outside_rejected(connect, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside)
    r = await call(connect, "run_command", {"command": "pwd", "cwd": "link"})
    assert r.is_error
    assert "outside the workspace" in text(r)


async def test_run_command_cwd_missing(connect):
    r = await call(connect, "run_command", {"command": "pwd", "cwd": "nope"})
    assert r.is_error
    assert "does not exist" in text(r)


async def test_run_command_cwd_is_file(connect, workspace):
    (workspace / "f.txt").write_text("x")
    r = await call(connect, "run_command", {"command": "pwd", "cwd": "f.txt"})
    assert r.is_error
    assert "not a directory" in text(r)


@pytest.mark.parametrize("command", ["", "   \n"])
async def test_run_command_empty_rejected(connect, command):
    r = await call(connect, "run_command", {"command": command})
    assert r.is_error
    assert "empty" in text(r)


async def test_run_command_env_is_passed(connect):
    r = await call(connect, "run_command", {"command": 'echo "v=$MY_VAR"', "env": {"MY_VAR": "42"}})
    assert "v=42" in text(r)


@pytest.mark.parametrize("name", ["1BAD", "has-dash", "a b", ""])
async def test_run_command_invalid_env_name_rejected(connect, name):
    r = await call(connect, "run_command", {"command": "true", "env": {name: "x"}})
    assert r.is_error
    assert "invalid environment variable name" in text(r)


async def test_run_command_env_nul_value_rejected(connect):
    r = await call(connect, "run_command", {"command": "true", "env": {"X": "a\0b"}})
    assert r.is_error
    assert "NUL" in text(r)


@pytest.mark.bwrap
async def test_run_command_env_does_not_reach_host_bwrap(connect, workspace, tmp_path):
    # LD_PRELOAD in the bwrap process itself would run agent-built code on the host.
    if not shutil.which("gcc"):
        pytest.skip("gcc not available")
    marker = tmp_path / "host_marker"
    (workspace / "evil.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void init(void) {"
        f' FILE *f = fopen("{marker}", "a"); if (f) {{ fputs("x", f); fclose(f); }} }}\n'
    )
    r = await call(connect, "run_command", {"command": "gcc -shared -fPIC -o evil.so evil.c"})
    assert text(r).startswith("exit_code=0"), text(r)
    r = await call(connect, "run_command", {"command": "true", "env": {"LD_PRELOAD": str(workspace / "evil.so")}})
    assert not r.is_error
    assert not marker.exists()


@pytest.mark.bwrap
async def test_run_command_host_secrets_not_inherited(connect, monkeypatch):
    monkeypatch.setenv("DEV_LINK_TEST_SECRET", "s3cret")
    r = await call(connect, "run_command", {"command": 'echo "[${DEV_LINK_TEST_SECRET:-}]"'})
    assert "[]" in text(r)
    assert "s3cret" not in text(r)


async def test_run_command_stdin(connect):
    r = await call(connect, "run_command", {"command": "tr a-z A-Z", "stdin": "shout\n"})
    assert "SHOUT" in text(r)


async def test_run_command_large_stdin_consumed(connect):
    r = await call(connect, "run_command", {"command": "wc -c", "stdin": "x" * 3_000_000, "timeout_s": 30})
    assert "3000000" in text(r)


async def test_run_command_large_stdin_echoed_completes(connect):
    # `cat` fills the stdout pipe while large stdin is still being written; both must flow.
    r = await call(connect, "run_command", {"command": "cat", "stdin": "x" * 3_000_000, "timeout_s": 30})
    assert not r.is_error
    assert text(r).startswith("exit_code=0"), text(r)[:200]
    assert "redirect" in text(r)


async def test_run_command_stdin_not_read_times_out_on_time(connect):
    # The command stops reading after 10 bytes; the pending stdin write must not outlive the timeout.
    started = time.monotonic()
    r = await call(
        connect, "run_command", {"command": "head -c 10 >/dev/null; sleep 30", "stdin": "x" * 3_000_000, "timeout_s": 1}
    )
    assert not r.is_error
    assert "TIMED OUT" in text(r)
    assert time.monotonic() - started < 5


async def test_run_command_tmp_persists_between_calls(connect):
    await call(connect, "run_command", {"command": "echo kept > /tmp/dev_link_shell_probe"})
    r = await call(connect, "run_command", {"command": "cat /tmp/dev_link_shell_probe"})
    assert "kept" in text(r)


async def test_run_command_nul_in_command_rejected(connect):
    r = await call(connect, "run_command", {"command": "echo a\0b"})
    assert r.is_error
    assert "NUL" in text(r)


async def test_run_command_without_stdin_gets_eof(connect):
    # A command reading stdin must not hang waiting for input.
    r = await call(connect, "run_command", {"command": "cat; echo done", "timeout_s": 10})
    assert "done" in text(r)
    assert "TIMED OUT" not in text(r)


async def test_run_command_timeout_kills(connect):
    r = await call(connect, "run_command", {"command": "echo start; sleep 30", "timeout_s": 0.5})
    assert not r.is_error
    out = text(r)
    assert "TIMED OUT" in out
    assert "start" in out


@pytest.mark.parametrize("timeout_s", [0, -1])
async def test_run_command_non_positive_timeout_rejected(connect, timeout_s):
    r = await call(connect, "run_command", {"command": "true", "timeout_s": timeout_s})
    assert r.is_error


@pytest.mark.bwrap
async def test_run_command_background_job_dies_on_return(connect, workspace):
    r = await call(
        connect,
        "run_command",
        {"command": "(sleep 1; echo alive > bg.txt) > /dev/null 2>&1 & echo launched", "timeout_s": 10},
    )
    assert "launched" in text(r)
    r = await call(connect, "run_command", {"command": "sleep 2"})
    assert not (workspace / "bg.txt").exists()


@pytest.mark.bwrap
async def test_run_command_cannot_write_outside_workspace(connect, tmp_path):
    target = tmp_path / "escape.txt"
    # Sandbox /tmp is a private directory under the state dir, so the write may succeed but must not land on the host.
    r = await call(connect, "run_command", {"command": f"mkdir -p {tmp_path} && echo x > {target}"})
    assert not r.is_error
    assert not target.exists()


async def test_run_command_system_dirs_read_only(connect):
    r = await call(connect, "run_command", {"command": "touch /usr/dev_link_probe"})
    assert not text(r).startswith("exit_code=0")


async def test_run_command_truncates_large_output(connect, rt):
    rt.config.limits.max_output_bytes = 4096
    r = await call(connect, "run_command", {"command": "seq 1 100000"})
    out = text(r)
    assert "output truncated" in out
    assert "bytes elided" in out
    assert "redirect to a file" in out
    assert out.count("\n") < 2000
    assert "\n100000" in out


async def test_run_command_small_output_has_no_hint(connect):
    r = await call(connect, "run_command", {"command": "echo hi"})
    assert "redirect to a file" not in text(r)


# run_code


async def test_run_code_python_with_args(connect, rt):
    if not sandbox_has(rt, "python3"):
        pytest.skip("python3 not on sandbox PATH")
    code = "import sys\nprint('args', sys.argv[1:])\n"
    r = await call(connect, "run_code", {"language": "python", "code": code, "args": ["a", "b c"]})
    assert not r.is_error
    assert "args ['a', 'b c']" in text(r)


async def test_run_code_python_error_exit(connect, rt):
    if not sandbox_has(rt, "python3"):
        pytest.skip("python3 not on sandbox PATH")
    r = await call(connect, "run_code", {"language": "python", "code": "raise SystemExit(5)"})
    assert not r.is_error
    assert text(r).startswith("exit_code=5")


async def test_run_code_bash_args_and_cwd(connect, workspace):
    code = 'echo "first=$1 second=$2"; pwd'
    r = await call(connect, "run_code", {"language": "bash", "code": code, "args": ["x", "-y"]})
    assert not r.is_error
    assert "first=x second=-y" in text(r)
    assert str(workspace) in text(r)


async def test_run_code_sh(connect):
    r = await call(connect, "run_code", {"language": "sh", "code": 'echo "sh:$#"', "args": ["1", "2"]})
    assert "sh:2" in text(r)


async def test_run_code_perl(connect, rt):
    if not sandbox_has(rt, "perl"):
        pytest.skip("perl not on sandbox PATH")
    r = await call(connect, "run_code", {"language": "perl", "code": 'print "p:@ARGV\\n";', "args": ["q"]})
    assert "p:q" in text(r)


async def test_run_code_node(connect, rt):
    if not sandbox_has(rt, "node"):
        pytest.skip("node not on sandbox PATH")
    code = "console.log('n:' + process.argv.slice(2).join(','))"
    r = await call(connect, "run_code", {"language": "node", "code": code, "args": ["a", "b"]})
    assert "n:a,b" in text(r)


@pytest.mark.parametrize("language", ["ruby", "php"])
async def test_run_code_missing_interpreter_or_runs(connect, rt, language):
    snippet = {"ruby": "puts 'r:' + ARGV.join(',')", "php": "<?php echo 'r:' . implode(',', array_slice($argv, 1));"}
    r = await call(connect, "run_code", {"language": language, "code": snippet[language], "args": ["z"]})
    if sandbox_has(rt, language):
        assert "r:z" in text(r)
    else:
        assert r.is_error
        assert "not installed" in text(r)


async def test_run_code_nul_in_args_rejected(connect):
    r = await call(connect, "run_code", {"language": "bash", "code": "echo $1", "args": ["a\0b"]})
    assert r.is_error
    assert "NUL" in text(r)


async def test_run_code_timeout(connect):
    r = await call(connect, "run_code", {"language": "bash", "code": "sleep 30", "timeout_s": 0.5})
    assert "TIMED OUT" in text(r)


async def test_run_code_unknown_language_rejected(connect):
    r = await call(connect, "run_code", {"language": "cobol", "code": "x"})
    assert r.is_error


async def test_run_code_empty_rejected(connect):
    r = await call(connect, "run_code", {"language": "bash", "code": "  "})
    assert r.is_error
    assert "empty" in text(r)


@pytest.mark.bwrap
async def test_run_code_cannot_write_outside_workspace(connect, tmp_path):
    target = tmp_path / "escape2.txt"
    await call(connect, "run_code", {"language": "bash", "code": f"mkdir -p {tmp_path} && echo x > {target}"})
    assert not target.exists()


# registration


async def test_tools_registered_with_annotations(connect, rt):
    async with connect("shell") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"run_command", "run_code"}
    for tool in tools.values():
        assert tool.annotations.destructive_hint is True
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.open_world_hint is rt.config.sandbox.network
    assert "start_process" in tools["run_command"].description


async def test_calls_are_audited(connect, rt):
    await call(connect, "run_command", {"command": "true"})
    events = rt.audit.query(kind="tool")
    assert [e["name"] for e in events] == ["run_command"]
