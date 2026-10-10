import os
import stat

import pytest
from conftest import text


@pytest.fixture
def tree(workspace):
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text("import os\n\ndef main():\n    print('hello')\n    return 0\n")
    (workspace / "src" / "pkg" / "util.py").write_text("def helper():\n    return 'Hello'\n")
    (workspace / "src" / "web.ts").write_text("export const hello = 1;\n")
    (workspace / "README.md").write_text("# Project\nsay hello\n")
    (workspace / ".hidden.py").write_text("hello = 'hidden'\n")
    (workspace / "build").mkdir()
    (workspace / "build" / "out.py").write_text("hello = 'ignored'\n")
    (workspace / ".gitignore").write_text("build/\n")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("hello in git\n")
    return workspace


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    (d / "secret.txt").write_text("hello secret\n")
    return d


async def call(connect, tool, args):
    async with connect("search") as client:
        return await client.call_tool(tool, args)


# --- grep ---------------------------------------------------------------------


async def test_grep_content_groups_matches_by_file(connect, tree):
    r = await call(connect, "grep", {"pattern": "hello"})
    assert not r.is_error, text(r)
    out = text(r)
    assert "src/app.py\n4:     print('hello')" in out
    assert "README.md\n2: say hello" in out
    assert "src/web.ts\n1: export const hello = 1;" in out
    # Case-sensitive by default, hidden and ignored files skipped.
    assert "util.py" not in out
    assert ".hidden.py" not in out
    assert "build/out.py" not in out
    assert ".git/config" not in out
    assert "3 matching lines in 3 files" in out
    # Files are sorted by path.
    assert out.index("README.md") < out.index("src/app.py") < out.index("src/web.ts")


async def test_grep_ignore_case_and_glob(connect, tree):
    r = await call(connect, "grep", {"pattern": "HELLO", "ignore_case": True, "glob": "*.py"})
    out = text(r)
    assert "src/app.py" in out and "src/pkg/util.py" in out
    assert "web.ts" not in out and "README.md" not in out


async def test_grep_glob_does_not_pull_in_hidden_files(connect, tree):
    out = text(await call(connect, "grep", {"pattern": "hello", "glob": "*.py", "output_mode": "files"}))
    assert ".hidden.py" not in out
    out = text(
        await call(
            connect, "grep", {"pattern": "hello", "glob": "*.py", "output_mode": "files", "include_hidden": True}
        )
    )
    assert ".hidden.py" in out


async def test_grep_explicit_hidden_path_is_searched(connect, workspace):
    (workspace / ".config").mkdir()
    (workspace / ".config" / "a.toml").write_text("key = 1\n")
    out = text(await call(connect, "grep", {"pattern": "key", "path": ".config"}))
    assert ".config/a.toml\n1: key = 1" in out


async def test_grep_glob_with_slash_is_workspace_relative(connect, tree):
    r = await call(connect, "grep", {"pattern": "hello", "ignore_case": True, "glob": "src/pkg/*.py"})
    out = text(r)
    assert "src/pkg/util.py" in out
    assert "src/app.py" not in out


async def test_grep_fixed_strings_treats_regex_chars_literally(connect, workspace):
    (workspace / "a.txt").write_text("a.b\naxb\n")
    r = await call(connect, "grep", {"pattern": "a.b", "fixed_strings": True})
    out = text(r)
    assert "1: a.b" in out and "axb" not in out
    r = await call(connect, "grep", {"pattern": "a.b"})
    assert "2: axb" in text(r)


async def test_grep_context_lines_and_separators(connect, workspace):
    lines = [f"line{i}" for i in range(1, 21)]
    lines[4] = "MATCH five"
    lines[14] = "MATCH fifteen"
    (workspace / "f.txt").write_text("\n".join(lines) + "\n")
    r = await call(connect, "grep", {"pattern": "MATCH", "context": 1})
    out = text(r)
    assert "4- line4\n5: MATCH five\n6- line6\n--\n14- line14\n15: MATCH fifteen\n16- line16" in out


async def test_grep_files_mode(connect, tree):
    r = await call(connect, "grep", {"pattern": "hello", "output_mode": "files"})
    out = text(r)
    assert out.splitlines()[:3] == ["README.md", "src/app.py", "src/web.ts"]
    assert "3 files" in out


async def test_grep_count_mode(connect, workspace):
    (workspace / "a.txt").write_text("x\nx\nx\n")
    (workspace / "b.txt").write_text("x\n")
    r = await call(connect, "grep", {"pattern": "x", "output_mode": "count"})
    assert text(r).splitlines()[:2] == ["a.txt: 3", "b.txt: 1"]


async def test_grep_hidden_and_ignored_opt_in(connect, tree):
    r = await call(connect, "grep", {"pattern": "hello", "include_hidden": True, "output_mode": "files"})
    out = text(r)
    assert ".hidden.py" in out
    assert ".git/config" not in out
    r = await call(connect, "grep", {"pattern": "hello", "include_ignored": True, "output_mode": "files"})
    assert "build/out.py" in text(r)


async def test_grep_subdirectory_and_single_file(connect, tree):
    r = await call(connect, "grep", {"pattern": "def", "path": "src/pkg"})
    assert "src/pkg/util.py\n1: def helper():" in text(r)
    assert "app.py" not in text(r)
    r = await call(connect, "grep", {"pattern": "return", "path": "src/app.py"})
    assert "src/app.py\n5:     return 0" in text(r)


async def test_grep_max_results_truncates_content(connect, workspace):
    (workspace / "a.txt").write_text("".join(f"hit {i}\n" for i in range(10)))
    (workspace / "b.txt").write_text("hit b\n")
    r = await call(connect, "grep", {"pattern": "hit", "max_results": 3})
    out = text(r)
    assert "1: hit 0" in out and "3: hit 2" in out
    assert "hit 3" not in out and "b.txt" not in out
    assert "showing the first 3 matching lines" in out


async def test_grep_max_results_truncates_files(connect, workspace):
    for i in range(5):
        (workspace / f"f{i}.txt").write_text("hit\n")
    r = await call(connect, "grep", {"pattern": "hit", "output_mode": "files", "max_results": 2})
    out = text(r)
    assert out.splitlines()[:2] == ["f0.txt", "f1.txt"]
    assert "f2.txt" not in out
    assert "showing 2 of 5 files" in out


async def test_grep_count_mode_single_file_keeps_path(connect, workspace):
    (workspace / "a.txt").write_text("x\nx\n")
    r = await call(connect, "grep", {"pattern": "x", "output_mode": "count", "path": "a.txt"})
    assert text(r).splitlines()[0] == "a.txt: 2"


async def test_grep_reports_truncation_when_one_file_exceeds_max_results(connect, workspace):
    (workspace / "a.txt").write_text("hit\n" * 10)
    out = text(await call(connect, "grep", {"pattern": "hit", "max_results": 3}))
    assert "3: hit" in out and "4: hit" not in out
    assert "showing the first 3 matching lines" in out


async def test_grep_exactly_max_results_is_not_truncated(connect, workspace):
    (workspace / "a.txt").write_text("hit\n" * 3)
    out = text(await call(connect, "grep", {"pattern": "hit", "max_results": 3}))
    assert "3 matching lines in 1 files" in out


async def test_grep_truncation_drops_leading_context_of_cut_match(connect, workspace):
    (workspace / "a.txt").write_text("hit\nc1\nc2\nc3\nc4\nhit\n")
    out = text(await call(connect, "grep", {"pattern": "hit", "max_results": 1, "context": 1}))
    assert "1: hit\n2- c1" in out
    assert "c3" not in out and "c4" not in out and "6: hit" not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read files with mode 000")
@pytest.mark.parametrize("mode", ["content", "files", "count"])
async def test_grep_unreadable_file_is_a_warning_not_a_failure(connect, workspace, mode):
    (workspace / "a.txt").write_text("hello\n")
    locked = workspace / "locked.txt"
    locked.write_text("hello\n")
    locked.chmod(0)
    try:
        r = await call(connect, "grep", {"pattern": "hello", "output_mode": mode})
        assert not r.is_error, text(r)
        assert "a.txt" in text(r) and "Permission denied" in text(r)
        r = await call(connect, "grep", {"pattern": "absent", "output_mode": mode})
        assert not r.is_error, text(r)
        assert "no matches" in text(r) and "Permission denied" in text(r)
    finally:
        locked.chmod(0o644)


async def test_grep_invalid_glob_is_error(connect, tree):
    r = await call(connect, "grep", {"pattern": "x", "glob": "{"})
    assert r.is_error and "glob" in text(r)


async def test_grep_long_lines_are_shortened(connect, workspace):
    (workspace / "min.js").write_text("hit" + "x" * 5000 + "\n")
    out = text(await call(connect, "grep", {"pattern": "hit"}))
    assert "more characters]" in out
    assert len(out) < 1000


async def test_grep_no_matches(connect, tree):
    r = await call(connect, "grep", {"pattern": "zzz_nothing"})
    assert not r.is_error
    assert "no matches" in text(r)


async def test_grep_pattern_starting_with_dash(connect, workspace):
    (workspace / "a.txt").write_text("--verbose flag\n")
    r = await call(connect, "grep", {"pattern": "--verbose"})
    assert not r.is_error, text(r)
    assert "1: --verbose flag" in text(r)


async def test_grep_invalid_regex_is_error(connect, tree):
    r = await call(connect, "grep", {"pattern": "foo("})
    assert r.is_error
    assert "fixed_strings" in text(r)


@pytest.mark.parametrize("bad", [{"pattern": ""}, {"pattern": "x", "context": 21}, {"pattern": "x", "max_results": 0}])
async def test_grep_rejects_bad_arguments(connect, tree, bad):
    assert (await call(connect, "grep", bad)).is_error


async def test_grep_rejects_unknown_output_mode(connect, tree):
    assert (await call(connect, "grep", {"pattern": "x", "output_mode": "lines"})).is_error


async def test_grep_missing_path_is_error(connect, tree):
    r = await call(connect, "grep", {"pattern": "x", "path": "nope"})
    assert r.is_error and "does not exist" in text(r)


@pytest.mark.parametrize("path_kind", ["dotdot", "absolute", "symlink"])
@pytest.mark.bwrap
async def test_grep_rejects_paths_outside_workspace(connect, workspace, outside, path_kind):
    if path_kind == "dotdot":
        path = "../outside"
    elif path_kind == "absolute":
        path = str(outside)
    else:
        (workspace / "link").symlink_to(outside)
        path = "link"
    r = await call(connect, "grep", {"pattern": "secret", "path": path})
    assert r.is_error
    assert "outside the workspace" in text(r)


async def test_grep_does_not_follow_symlink_out_of_workspace(connect, workspace, outside):
    (workspace / "link").symlink_to(outside)
    (workspace / "inside.txt").write_text("hello inside\n")
    out = text(await call(connect, "grep", {"pattern": "hello"}))
    assert "inside.txt" in out
    assert "secret" not in out


# --- find_files ---------------------------------------------------------------


async def test_find_files_by_name(connect, tree):
    r = await call(connect, "find_files", {"pattern": "*.py"})
    assert not r.is_error, text(r)
    out = text(r)
    assert out.splitlines()[:2] == ["src/app.py", "src/pkg/util.py"]
    assert ".hidden.py" not in out and "build/out.py" not in out
    assert "2 files" in out


async def test_find_files_path_glob_and_subdir(connect, tree):
    out = text(await call(connect, "find_files", {"pattern": "src/pkg/**"}))
    assert out.splitlines()[0] == "src/pkg/util.py"
    assert "src/app.py" not in out
    out = text(await call(connect, "find_files", {"pattern": "*.ts", "path": "src"}))
    assert out.splitlines()[0] == "src/web.ts"


async def test_find_files_ignore_case(connect, tree):
    assert "no files match" in text(await call(connect, "find_files", {"pattern": "readme.md"}))
    out = text(await call(connect, "find_files", {"pattern": "readme.md", "ignore_case": True}))
    assert out.splitlines()[0] == "README.md"


async def test_find_files_hidden_and_ignored(connect, tree):
    out = text(await call(connect, "find_files", {"pattern": "*.py", "include_hidden": True, "include_ignored": True}))
    assert ".hidden.py" in out and "build/out.py" in out
    out = text(await call(connect, "find_files", {"pattern": "config", "include_hidden": True}))
    assert ".git/config" not in out


async def test_find_files_max_results(connect, workspace):
    for i in range(6):
        (workspace / f"f{i}.txt").write_text("")
    out = text(await call(connect, "find_files", {"pattern": "*.txt", "max_results": 4}))
    assert out.splitlines()[:4] == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
    assert "f4.txt" not in out
    assert "showing 4 of 6 files" in out


async def test_find_files_errors(connect, tree):
    assert (await call(connect, "find_files", {"pattern": ""})).is_error
    r = await call(connect, "find_files", {"pattern": "*", "path": "README.md"})
    assert r.is_error and "not a directory" in text(r)
    assert (await call(connect, "find_files", {"pattern": "*", "path": "missing"})).is_error


@pytest.mark.parametrize("path_kind", ["dotdot", "absolute", "symlink"])
@pytest.mark.bwrap
async def test_find_files_rejects_paths_outside_workspace(connect, workspace, outside, path_kind):
    if path_kind == "dotdot":
        path = "../outside"
    elif path_kind == "absolute":
        path = str(outside)
    else:
        (workspace / "link").symlink_to(outside)
        path = "link"
    r = await call(connect, "find_files", {"pattern": "*", "path": path})
    assert r.is_error and "outside the workspace" in text(r)


# --- replace_in_files ---------------------------------------------------------


async def test_replace_dry_run_shows_diff_and_writes_nothing(connect, tree):
    before = (tree / "src" / "app.py").read_text()
    r = await call(connect, "replace_in_files", {"pattern": "hello", "replacement": "bye"})
    assert not r.is_error, text(r)
    out = text(r)
    assert out.startswith("DRY RUN: 3 replacements in 3 files")
    assert "--- a/src/app.py\n+++ b/src/app.py" in out
    assert "-    print('hello')\n+    print('bye')" in out
    assert "  README.md: 1" in out
    assert (tree / "src" / "app.py").read_text() == before
    # Ignored and hidden files are out of scope.
    assert "build/out.py" not in out and ".hidden.py" not in out


async def test_replace_writes_when_not_dry_run(connect, tree):
    mode = (tree / "src" / "app.py").stat().st_mode
    r = await call(connect, "replace_in_files", {"pattern": "hello", "replacement": "bye", "dry_run": False})
    assert not r.is_error, text(r)
    assert text(r).startswith("3 replacements written in 3 files.")
    assert (tree / "src" / "app.py").read_text() == "import os\n\ndef main():\n    print('bye')\n    return 0\n"
    assert (tree / "README.md").read_text() == "# Project\nsay bye\n"
    assert (tree / "build" / "out.py").read_text() == "hello = 'ignored'\n"
    assert (tree / ".hidden.py").read_text() == "hello = 'hidden'\n"
    assert (tree / "src" / "app.py").stat().st_mode == mode
    # No temp files left behind.
    assert not [p for p in tree.rglob("*.tmp")]


async def test_replace_preserves_executable_bit(connect, workspace):
    script = workspace / "run.sh"
    script.write_text("echo old\n")
    script.chmod(0o755)
    await call(connect, "replace_in_files", {"pattern": "old", "replacement": "new", "dry_run": False})
    assert script.read_text() == "echo new\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


async def test_replace_regex_groups(connect, workspace):
    (workspace / "a.py").write_text("foo(1)\nfoo(22)\n")
    await call(
        connect,
        "replace_in_files",
        {"pattern": r"foo\((\d+)\)", "replacement": r"bar(\1, x=\g<1>)", "dry_run": False},
    )
    assert (workspace / "a.py").read_text() == "bar(1, x=1)\nbar(22, x=22)\n"


async def test_replace_literal_mode_escapes_pattern_and_replacement(connect, workspace):
    (workspace / "a.txt").write_text("a.b axb\n")
    await call(
        connect,
        "replace_in_files",
        {"pattern": "a.b", "replacement": r"C:\new\1", "regex": False, "dry_run": False},
    )
    assert (workspace / "a.txt").read_text() == "C:\\new\\1 axb\n"


async def test_replace_ignore_case_glob_and_path(connect, tree):
    r = await call(
        connect,
        "replace_in_files",
        {"pattern": "hello", "replacement": "hi", "ignore_case": True, "glob": "*.py", "path": "src"},
    )
    out = text(r)
    assert "src/app.py: 1" in out and "src/pkg/util.py: 1" in out
    assert "web.ts" not in out and "README.md" not in out


async def test_replace_single_file_path(connect, tree):
    out = text(await call(connect, "replace_in_files", {"pattern": "hello", "replacement": "x", "path": "README.md"}))
    assert "README.md: 1" in out and "src/app.py" not in out


async def test_replace_preserves_crlf_and_missing_final_newline(connect, workspace):
    (workspace / "a.txt").write_bytes(b"one\r\ntwo")
    r = await call(connect, "replace_in_files", {"pattern": "two", "replacement": "2", "dry_run": False})
    assert "\\ No newline at end of file" in text(r)
    assert (workspace / "a.txt").read_bytes() == b"one\r\n2"


async def test_replace_multiline_pattern(connect, workspace):
    (workspace / "a.txt").write_text("start\nmiddle\nend\n")
    await call(connect, "replace_in_files", {"pattern": r"start\nmiddle", "replacement": "joined", "dry_run": False})
    assert (workspace / "a.txt").read_text() == "joined\nend\n"


async def test_replace_skips_binary_non_utf8_and_large_files(connect, workspace, rt):
    (workspace / "bin.dat").write_bytes(b"hello\0\x01\x02")
    (workspace / "latin.txt").write_bytes("hello caf\xe9\n".encode("latin-1"))
    rt.config.limits.max_read_bytes = 100
    (workspace / "big.txt").write_text("hello " * 50)
    (workspace / "ok.txt").write_text("hello\n")
    r = await call(connect, "replace_in_files", {"pattern": "hello", "replacement": "bye", "dry_run": False})
    out = text(r)
    assert "1 replacements written in 1 files" in out
    assert "latin.txt: not UTF-8 text" in out
    assert "big.txt: larger than 100 bytes" in out
    assert (workspace / "bin.dat").read_bytes() == b"hello\0\x01\x02"
    assert (workspace / "ok.txt").read_text() == "bye\n"


async def test_replace_no_matches(connect, tree):
    r = await call(connect, "replace_in_files", {"pattern": "zzz", "replacement": "y"})
    assert not r.is_error and "no matches" in text(r)


async def test_replace_invalid_regex_and_replacement(connect, tree):
    r = await call(connect, "replace_in_files", {"pattern": "foo(", "replacement": "x"})
    assert r.is_error and "regex=false" in text(r)
    r = await call(connect, "replace_in_files", {"pattern": "hello", "replacement": r"\9"})
    assert r.is_error and "invalid replacement" in text(r)
    assert (await call(connect, "replace_in_files", {"pattern": "", "replacement": "x"})).is_error


@pytest.mark.parametrize("path_kind", ["dotdot", "absolute", "symlink"])
@pytest.mark.bwrap
async def test_replace_rejects_paths_outside_workspace(connect, workspace, outside, path_kind):
    if path_kind == "dotdot":
        path = "../outside"
    elif path_kind == "absolute":
        path = str(outside)
    else:
        (workspace / "link").symlink_to(outside)
        path = "link"
    r = await call(
        connect, "replace_in_files", {"pattern": "hello", "replacement": "x", "path": path, "dry_run": False}
    )
    assert r.is_error and "outside the workspace" in text(r)
    assert (outside / "secret.txt").read_text() == "hello secret\n"


async def test_replace_never_writes_through_symlink_out_of_workspace(connect, workspace, outside):
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    (workspace / "dirlink").symlink_to(outside)
    (workspace / "ok.txt").write_text("hello\n")
    r = await call(connect, "replace_in_files", {"pattern": "hello", "replacement": "x", "dry_run": False})
    assert not r.is_error, text(r)
    assert (outside / "secret.txt").read_text() == "hello secret\n"
    assert (workspace / "ok.txt").read_text() == "x\n"
    assert os.path.islink(workspace / "link.txt")


async def test_tools_are_registered_with_annotations(connect):
    async with connect("search") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"grep", "find_files", "replace_in_files"}
    assert tools["grep"].annotations.read_only_hint is True
    assert tools["find_files"].annotations.read_only_hint is True
    assert tools["replace_in_files"].annotations.read_only_hint is False


async def test_replace_runaway_pattern_is_stopped_at_the_timeout(connect, workspace, rt):
    # Catastrophic backtracking would run for hours; the regex worker is killed at the deadline.
    rt.config.limits.default_timeout_s = 1
    (workspace / "a.txt").write_text("a" * 34 + "!\n")
    r = await call(connect, "replace_in_files", {"pattern": "^(a+)+$", "replacement": "x"})
    assert r.is_error
    assert "did not finish within 1s" in text(r)
    assert (workspace / "a.txt").read_text() == "a" * 34 + "!\n"


async def test_replace_works_across_batches(connect, workspace, monkeypatch):
    import dev_link_mcp.tools.search as search_module

    monkeypatch.setattr(search_module, "_REPLACE_BATCH_BYTES", 10)
    for i in range(5):
        (workspace / f"f{i}.txt").write_text(f"hello {i}\n")
    (workspace / "skip.txt").write_text("nothing\n")
    r = await call(connect, "replace_in_files", {"pattern": "hello", "replacement": "bye", "dry_run": False})
    assert not r.is_error, text(r)
    assert [(workspace / f"f{i}.txt").read_text() for i in range(5)] == [f"bye {i}\n" for i in range(5)]
    assert (workspace / "skip.txt").read_text() == "nothing\n"
