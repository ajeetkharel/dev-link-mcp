import asyncio
import base64
import os
import subprocess

import mcp.types as types
import pytest
from conftest import text

from dev_link_mcp.tools.files import patch_paths

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def outside(tmp_path):
    """A secret file next to (not inside) the workspace."""
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n")
    return secret


async def call(connect, tool, args):
    async with connect("files") as client:
        return await client.call_tool(tool, args)


async def assert_escape_rejected(connect, workspace, outside, tool, make_args):
    """The tool refuses `../`, an absolute path outside, and a symlink pointing outside."""
    (workspace / "link_out").symlink_to(outside)
    for bad in ("../secret.txt", str(outside), "link_out"):
        r = await call(connect, tool, make_args(bad))
        assert r.is_error, (tool, bad, text(r))
        assert "outside the workspace" in text(r), (tool, bad, text(r))


# --- read_file -------------------------------------------------------------


async def test_read_file_numbers_lines_and_reports_total(connect, workspace):
    (workspace / "a.txt").write_text("alpha\nbeta\ngamma\n")
    r = await call(connect, "read_file", {"path": "a.txt"})
    assert not r.is_error
    out = text(r)
    assert out.splitlines()[0] == "a.txt: lines 1-3 of 3"
    assert "     1\talpha" in out
    assert "     3\tgamma" in out
    assert "more lines" not in out


async def test_read_file_offset_limit_and_continuation_hint(connect, workspace):
    (workspace / "n.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    r = await call(connect, "read_file", {"path": "n.txt", "offset": 4, "limit": 3})
    out = text(r)
    assert out.splitlines()[0] == "n.txt: lines 4-6 of 10"
    assert "     4\tline4" in out and "     6\tline6" in out
    assert "line7" not in out and "line3" not in out
    assert "continue with offset=7" in out


async def test_read_file_offset_past_end_is_error(connect, workspace):
    (workspace / "n.txt").write_text("one\ntwo\n")
    r = await call(connect, "read_file", {"path": "n.txt", "offset": 5})
    assert r.is_error
    assert "past the end" in text(r) and "2 lines" in text(r)


async def test_read_file_rejects_invalid_offset(connect, workspace):
    (workspace / "n.txt").write_text("one\n")
    r = await call(connect, "read_file", {"path": "n.txt", "offset": 0})
    assert r.is_error


async def test_read_file_empty_and_no_trailing_newline(connect, workspace):
    (workspace / "empty.txt").write_text("")
    (workspace / "nonl.txt").write_text("a\r\nb")
    assert "(empty file)" in text(await call(connect, "read_file", {"path": "empty.txt"}))
    out = text(await call(connect, "read_file", {"path": "nonl.txt"}))
    assert "lines 1-2 of 2" in out
    assert "     1\ta\n" in out and "\r" not in out


async def test_read_file_truncates_long_lines(connect, workspace):
    (workspace / "min.js").write_text("x" * 200_000 + "\nsecond\n")
    out = text(await call(connect, "read_file", {"path": "min.js"}))
    assert "[line truncated]" in out
    assert "lines 1-2 of 2" in out
    assert "     2\tsecond" in out
    assert len(out) < 5000


async def test_read_file_stops_at_output_limit(connect, workspace, rt):
    rt.config.limits.max_output_bytes = 2000
    (workspace / "big.txt").write_text("".join(f"row {i:05d} {'y' * 50}\n" for i in range(1000)))
    out = text(await call(connect, "read_file", {"path": "big.txt"}))
    assert "of 1000" in out
    assert "output size limit reached; continue with offset=" in out
    assert len(out) < 3000


async def test_read_file_refuses_binary(connect, workspace):
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02binary\xff" * 100)
    r = await call(connect, "read_file", {"path": "blob.bin"})
    assert r.is_error
    assert "binary" in text(r) and "file_info" in text(r)


async def test_read_file_returns_image_content(connect, workspace):
    (workspace / "pic.png").write_bytes(PNG)
    r = await call(connect, "read_file", {"path": "pic.png"})
    assert not r.is_error
    images = [b for b in r.content if isinstance(b, types.ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert base64.b64decode(images[0].data) == PNG


async def test_read_file_image_over_limit(connect, workspace, rt):
    rt.config.limits.max_read_bytes = 10
    (workspace / "pic.png").write_bytes(PNG)
    r = await call(connect, "read_file", {"path": "pic.png"})
    assert r.is_error and "read limit" in text(r)


async def test_read_file_errors_for_missing_and_directory(connect, workspace):
    (workspace / "d").mkdir()
    assert "does not exist" in text(await call(connect, "read_file", {"path": "nope.txt"}))
    r = await call(connect, "read_file", {"path": "d"})
    assert r.is_error and "directory" in text(r)


async def test_read_file_follows_symlink_inside_workspace(connect, workspace):
    (workspace / "real.txt").write_text("hello\n")
    (workspace / "alias.txt").symlink_to(workspace / "real.txt")
    out = text(await call(connect, "read_file", {"path": "alias.txt"}))
    assert "real.txt: lines 1-1 of 1" in out


@pytest.mark.bwrap
async def test_read_file_path_escape(connect, workspace, outside):
    await assert_escape_rejected(connect, workspace, outside, "read_file", lambda p: {"path": p})


# --- read_many_files -------------------------------------------------------


@pytest.mark.bwrap
async def test_read_many_files_mixed_results(connect, workspace, outside):
    (workspace / "a.txt").write_text("A1\nA2\nA3\n")
    (workspace / "b.txt").write_text("B1\n")
    (workspace / "pic.png").write_bytes(PNG)
    (workspace / "blob.bin").write_bytes(b"\x00" * 10)
    r = await call(
        connect,
        "read_many_files",
        {"paths": ["a.txt", "b.txt", "missing.txt", "pic.png", "blob.bin", "../secret.txt"], "limit_per_file": 2},
    )
    assert not r.is_error
    out = text(r)
    assert "==> a.txt <==\na.txt: lines 1-2 of 3" in out
    assert "continue with offset=3" in out
    assert "     1\tB1" in out
    assert "==> missing.txt <==\nerror: path does not exist" in out
    assert "use read_file to view it" in out
    assert "==> blob.bin <==\nerror:" in out and "binary" in out
    assert "outside the workspace" in out
    assert "top secret" not in out


async def test_read_many_files_respects_total_budget(connect, workspace, rt):
    rt.config.limits.max_output_bytes = 500
    for name in ("a", "b", "c"):
        (workspace / f"{name}.txt").write_text("z" * 80 + "\n" * 1 + ("q" * 80 + "\n") * 20)
    out = text(await call(connect, "read_many_files", {"paths": ["a.txt", "b.txt", "c.txt"]}))
    assert "skipped: output size limit reached" in out


async def test_read_many_files_requires_paths(connect):
    r = await call(connect, "read_many_files", {"paths": []})
    assert r.is_error


# --- write_file ------------------------------------------------------------


async def test_write_file_creates_with_parent_dirs(connect, workspace):
    r = await call(connect, "write_file", {"path": "src/pkg/mod.py", "content": "x = 1\ny = 2\n"})
    assert not r.is_error
    assert text(r) == "created src/pkg/mod.py (12 bytes, 2 lines)"
    assert (workspace / "src/pkg/mod.py").read_text() == "x = 1\ny = 2\n"


async def test_write_file_without_create_dirs(connect, workspace):
    r = await call(connect, "write_file", {"path": "nodir/f.txt", "content": "x", "create_dirs": False})
    assert r.is_error and "create_dirs" in text(r)
    assert not (workspace / "nodir").exists()


async def test_write_file_overwrites_atomically_and_keeps_mode(connect, workspace):
    target = workspace / "run.sh"
    target.write_text("old\n")
    target.chmod(0o755)
    r = await call(connect, "write_file", {"path": "run.sh", "content": "#!/bin/sh\r\necho hi\r\n"})
    assert text(r).startswith("overwrote run.sh")
    assert target.read_bytes() == b"#!/bin/sh\r\necho hi\r\n"
    assert target.stat().st_mode & 0o777 == 0o755
    assert [p.name for p in workspace.iterdir()] == ["run.sh"]


async def test_write_file_refuses_directory(connect, workspace):
    (workspace / "d").mkdir()
    r = await call(connect, "write_file", {"path": "d", "content": "x"})
    assert r.is_error and "directory" in text(r)


@pytest.mark.bwrap
async def test_write_file_path_escape(connect, workspace, outside):
    await assert_escape_rejected(connect, workspace, outside, "write_file", lambda p: {"path": p, "content": "pwned"})
    assert outside.read_text() == "top secret\n"
    r = await call(connect, "write_file", {"path": "../new_outside.txt", "content": "x"})
    assert r.is_error
    assert not (workspace.parent / "new_outside.txt").exists()


# --- edit_file -------------------------------------------------------------


async def test_edit_file_replaces_unique_match_and_returns_diff(connect, workspace):
    (workspace / "m.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    r = await call(connect, "edit_file", {"path": "m.py", "old_string": "return 1", "new_string": "return 10"})
    assert not r.is_error
    out = text(r)
    assert out.startswith("edited m.py: 1 replacement")
    assert "--- a/m.py" in out and "+++ b/m.py" in out
    assert "-    return 1" in out and "+    return 10" in out
    assert (workspace / "m.py").read_text() == "def f():\n    return 10\n\n\ndef g():\n    return 2\n"


async def test_edit_file_not_found(connect, workspace):
    (workspace / "m.py").write_text("a\n")
    r = await call(connect, "edit_file", {"path": "m.py", "old_string": "zzz", "new_string": "y"})
    assert r.is_error and "not found" in text(r)


async def test_edit_file_ambiguous_then_replace_all(connect, workspace):
    (workspace / "m.py").write_text("x = 1\nx = 1\nx = 1\n")
    r = await call(connect, "edit_file", {"path": "m.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert r.is_error and "matches 3 places" in text(r)
    assert (workspace / "m.py").read_text() == "x = 1\nx = 1\nx = 1\n"
    r = await call(
        connect, "edit_file", {"path": "m.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}
    )
    assert not r.is_error and "3 replacements" in text(r)
    assert (workspace / "m.py").read_text() == "x = 2\nx = 2\nx = 2\n"


async def test_edit_file_rejects_empty_and_identical(connect, workspace):
    (workspace / "m.py").write_text("a\n")
    r = await call(connect, "edit_file", {"path": "m.py", "old_string": "", "new_string": "y"})
    assert r.is_error and "empty" in text(r)
    r = await call(connect, "edit_file", {"path": "m.py", "old_string": "a", "new_string": "a"})
    assert r.is_error and "identical" in text(r)


async def test_edit_file_preserves_crlf(connect, workspace):
    (workspace / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    r = await call(connect, "edit_file", {"path": "w.txt", "old_string": "one\ntwo", "new_string": "1\n2"})
    assert not r.is_error, text(r)
    assert (workspace / "w.txt").read_bytes() == b"1\r\n2\r\nthree\r\n"


async def test_edit_file_refuses_binary(connect, workspace):
    (workspace / "b.bin").write_bytes(b"\xff\xfe\x00abc")
    r = await call(connect, "edit_file", {"path": "b.bin", "old_string": "abc", "new_string": "x"})
    assert r.is_error and "not UTF-8" in text(r)


@pytest.mark.bwrap
async def test_edit_file_path_escape(connect, workspace, outside):
    await assert_escape_rejected(
        connect, workspace, outside, "edit_file", lambda p: {"path": p, "old_string": "top", "new_string": "x"}
    )
    assert outside.read_text() == "top secret\n"


# --- multi_edit ------------------------------------------------------------


async def test_multi_edit_applies_in_order(connect, workspace):
    (workspace / "c.py").write_text("a = 1\nb = 2\nc = 3\n")
    edits = [
        {"old_string": "a = 1", "new_string": "a = 100"},
        {"old_string": "a = 100\nb = 2", "new_string": "a = 100\nb = 200"},
        {"old_string": "= 3", "new_string": "= 300", "replace_all": True},
    ]
    r = await call(connect, "multi_edit", {"path": "c.py", "edits": edits})
    assert not r.is_error, text(r)
    assert "3 replacements" in text(r)
    assert (workspace / "c.py").read_text() == "a = 100\nb = 200\nc = 300\n"


async def test_multi_edit_is_all_or_nothing(connect, workspace):
    (workspace / "c.py").write_text("a = 1\nb = 2\n")
    edits = [{"old_string": "a = 1", "new_string": "a = 9"}, {"old_string": "missing", "new_string": "x"}]
    r = await call(connect, "multi_edit", {"path": "c.py", "edits": edits})
    assert r.is_error and "edit 2" in text(r)
    assert (workspace / "c.py").read_text() == "a = 1\nb = 2\n"


async def test_multi_edit_requires_edits(connect, workspace):
    (workspace / "c.py").write_text("a\n")
    r = await call(connect, "multi_edit", {"path": "c.py", "edits": []})
    assert r.is_error


@pytest.mark.bwrap
async def test_multi_edit_path_escape(connect, workspace, outside):
    await assert_escape_rejected(
        connect,
        workspace,
        outside,
        "multi_edit",
        lambda p: {"path": p, "edits": [{"old_string": "top", "new_string": "x"}]},
    )


# --- apply_patch -----------------------------------------------------------

PATCH = """\
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
diff --git a/new/file.txt b/new/file.txt
new file mode 100644
--- /dev/null
+++ b/new/file.txt
@@ -0,0 +1,2 @@
+fresh
+content
"""


async def test_apply_patch_dry_run_changes_nothing(connect, workspace):
    (workspace / "hello.txt").write_text("one\ntwo\nthree\n")
    r = await call(connect, "apply_patch", {"patch": PATCH, "dry_run": True})
    assert not r.is_error, text(r)
    assert "applies cleanly" in text(r)
    assert "hello.txt" in text(r) and "new/file.txt" in text(r)
    assert (workspace / "hello.txt").read_text() == "one\ntwo\nthree\n"
    assert not (workspace / "new").exists()


async def test_apply_patch_applies_multiple_files(connect, workspace):
    (workspace / "hello.txt").write_text("one\ntwo\nthree\n")
    r = await call(connect, "apply_patch", {"patch": PATCH})
    assert not r.is_error, text(r)
    assert (workspace / "hello.txt").read_text() == "one\nTWO\nthree\n"
    assert (workspace / "new/file.txt").read_text() == "fresh\ncontent\n"


async def test_apply_patch_tolerates_wrong_counts_and_missing_newline(connect, workspace):
    (workspace / "hello.txt").write_text("one\ntwo\nthree\n")
    patch = "--- a/hello.txt\n+++ b/hello.txt\n@@ -1,9 +1,9 @@\n one\n-two\n+2\n three"
    r = await call(connect, "apply_patch", {"patch": patch})
    assert not r.is_error, text(r)
    assert (workspace / "hello.txt").read_text() == "one\n2\nthree\n"


async def test_apply_patch_strip_zero(connect, workspace):
    (workspace / "hello.txt").write_text("one\ntwo\nthree\n")
    patch = "--- hello.txt\n+++ hello.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+zwei\n three\n"
    r = await call(connect, "apply_patch", {"patch": patch, "strip": 0})
    assert not r.is_error, text(r)
    assert (workspace / "hello.txt").read_text() == "one\nzwei\nthree\n"


async def test_apply_patch_is_atomic_on_failure(connect, workspace):
    (workspace / "hello.txt").write_text("one\ntwo\nthree\n")
    (workspace / "other.txt").write_text("unrelated\n")
    patch = PATCH + "--- a/other.txt\n+++ b/other.txt\n@@ -1 +1 @@\n-does not match\n+x\n"
    r = await call(connect, "apply_patch", {"patch": patch})
    assert r.is_error
    assert "nothing was changed" in text(r)
    assert (workspace / "hello.txt").read_text() == "one\ntwo\nthree\n"
    assert not (workspace / "new").exists()


async def test_apply_patch_deletes_and_renames(connect, workspace):
    (workspace / "gone.txt").write_text("bye\n")
    (workspace / "old.txt").write_text("keep\n")
    patch = (
        "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-bye\n"
        "diff --git a/old.txt b/renamed.txt\nsimilarity index 100%\nrename from old.txt\nrename to renamed.txt\n"
    )
    r = await call(connect, "apply_patch", {"patch": patch})
    assert not r.is_error, text(r)
    assert not (workspace / "gone.txt").exists()
    assert (workspace / "renamed.txt").read_text() == "keep\n"


async def test_apply_patch_works_inside_git_repo(connect, workspace):
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / "hello.txt").write_text("one\ntwo\nthree\n")
    r = await call(connect, "apply_patch", {"patch": PATCH})
    assert not r.is_error, text(r)
    assert (workspace / "hello.txt").read_text() == "one\nTWO\nthree\n"


async def test_apply_patch_requires_headers(connect):
    r = await call(connect, "apply_patch", {"patch": "just some text\n"})
    assert r.is_error and "no file headers" in text(r)


@pytest.mark.bwrap
async def test_apply_patch_rejects_escaping_paths(connect, workspace, outside):
    (workspace / "link_dir").symlink_to(outside.parent)
    for target in ("../secret.txt", "link_dir/secret.txt"):
        patch = f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-top secret\n+pwned\n"
        r = await call(connect, "apply_patch", {"patch": patch})
        assert r.is_error and "outside the workspace" in text(r), text(r)
    patch = f"--- {outside}\n+++ {outside}\n@@ -1 +1 @@\n-top secret\n+pwned\n"
    r = await call(connect, "apply_patch", {"patch": patch, "strip": 0})
    assert r.is_error and "outside the workspace" in text(r)
    patch = "diff --git a/x b/y\nrename from x\nrename to ../escaped\n"
    r = await call(connect, "apply_patch", {"patch": patch})
    assert r.is_error and "outside the workspace" in text(r)
    assert outside.read_text() == "top secret\n"


def test_patch_paths_ignores_removed_lines_that_look_like_headers():
    patch = "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n select 1;\n--- a comment\n+++ added\n select 2;\n"
    assert patch_paths(patch, 1) == ["q.sql"]


def test_patch_paths_handles_quoted_and_dev_null():
    patch = 'diff --git "a/sp ace.txt" "b/sp ace.txt"\n--- /dev/null\n+++ "b/sp ace.txt"\n@@ -0,0 +1 @@\n+x\n'
    assert patch_paths(patch, 1) == ["sp ace.txt"]


# --- list_dir / tree -------------------------------------------------------


async def test_list_dir_sorts_and_hides_dotfiles(connect, workspace):
    (workspace / "b.txt").write_text("12345")
    (workspace / "A.txt").write_text("")
    (workspace / "zdir").mkdir()
    (workspace / ".hidden").write_text("")
    (workspace / "ln").symlink_to("b.txt")
    out = text(await call(connect, "list_dir", {}))
    lines = out.splitlines()
    assert lines[0] == ".: 4 entries (1 hidden; set show_hidden=true to see them)"
    assert lines[1].startswith("dir") and lines[1].endswith("zdir/")
    assert "file       5B b.txt" in out
    assert "ln -> b.txt" in out
    assert ".hidden" not in out
    out = text(await call(connect, "list_dir", {"path": ".", "show_hidden": True}))
    assert ".hidden" in out and "5 entries" in out


@pytest.mark.bwrap
async def test_list_dir_errors(connect, workspace, outside):
    (workspace / "f.txt").write_text("")
    r = await call(connect, "list_dir", {"path": "f.txt"})
    assert r.is_error and "not a directory" in text(r)
    r = await call(connect, "list_dir", {"path": "missing"})
    assert r.is_error
    await assert_escape_rejected(connect, workspace, outside.parent, "list_dir", lambda p: {"path": p})


async def test_tree_skips_vendor_dirs_and_limits_depth(connect, workspace):
    (workspace / "src/pkg/deep/deeper").mkdir(parents=True)
    (workspace / "src/pkg/mod.py").write_text("")
    (workspace / "src/pkg/deep/deeper/x.py").write_text("")
    (workspace / "node_modules/lib").mkdir(parents=True)
    (workspace / ".git").mkdir()
    (workspace / "README.md").write_text("")
    out = text(await call(connect, "tree", {"max_depth": 3}))
    assert out.splitlines()[0] == "./"
    assert "node_modules/ (skipped)" in out and "lib" not in out
    assert ".git/ (skipped)" in out
    assert "  src/\n    pkg/\n      deep/ ...\n      mod.py" in out
    assert "x.py" not in out
    assert "README.md" in out


async def test_tree_max_entries_and_subpath(connect, workspace):
    for i in range(20):
        (workspace / "many" / f"f{i:02d}").parent.mkdir(exist_ok=True)
        (workspace / "many" / f"f{i:02d}").write_text("")
    out = text(await call(connect, "tree", {"path": "many", "max_entries": 5}))
    assert out.splitlines()[0] == "many/"
    assert "f04" in out and "f05" not in out
    assert "stopped at 5 entries" in out


async def test_tree_does_not_follow_symlinked_dirs(connect, workspace, outside):
    (workspace / "out").symlink_to(outside.parent)
    out = text(await call(connect, "tree", {}))
    assert f"out -> {outside.parent}" in out
    assert "secret.txt" not in out


@pytest.mark.bwrap
async def test_tree_path_escape(connect, workspace, outside):
    await assert_escape_rejected(connect, workspace, outside.parent, "tree", lambda p: {"path": p})


# --- file_info -------------------------------------------------------------


async def test_file_info_text_file(connect, workspace):
    (workspace / "a.py").write_text("x\ny\nz\n")
    r = await call(connect, "file_info", {"path": "a.py"})
    assert not r.is_error
    info = r.structured_content
    assert info["path"] == "a.py"
    assert info["type"] == "file"
    assert info["size"] == 6
    assert info["lines"] == 3
    assert info["binary"] is False
    assert info["mime_type"] == "text/x-python"
    assert info["mode"].startswith("0o")


async def test_file_info_binary_dir_and_symlink(connect, workspace):
    (workspace / "b.bin").write_bytes(b"\x00\x01")
    (workspace / "d").mkdir()
    (workspace / "d/one").write_text("")
    (workspace / "ln").symlink_to("d")
    binary = (await call(connect, "file_info", {"path": "b.bin"})).structured_content
    assert binary["binary"] is True and "lines" not in binary
    directory = (await call(connect, "file_info", {"path": "d"})).structured_content
    assert directory["type"] == "directory" and directory["entries"] == 1
    link = (await call(connect, "file_info", {"path": "ln"})).structured_content
    assert link["path"] == "d"
    assert link["symlink"] == {"path": "ln", "target": "d"}


@pytest.mark.bwrap
async def test_file_info_path_escape(connect, workspace, outside):
    await assert_escape_rejected(connect, workspace, outside, "file_info", lambda p: {"path": p})


# --- make_dir --------------------------------------------------------------


async def test_make_dir(connect, workspace):
    r = await call(connect, "make_dir", {"path": "a/b/c"})
    assert text(r) == "created a/b/c/"
    assert (workspace / "a/b/c").is_dir()
    assert text(await call(connect, "make_dir", {"path": "a/b"})) == "a/b already exists"
    (workspace / "f").write_text("")
    r = await call(connect, "make_dir", {"path": "f"})
    assert r.is_error and "not a directory" in text(r)


@pytest.mark.bwrap
async def test_make_dir_path_escape(connect, workspace, outside):
    await assert_escape_rejected(connect, workspace, outside.parent, "make_dir", lambda p: {"path": p + "/x"})
    assert not (outside.parent / "x").exists()


# --- move_path -------------------------------------------------------------


async def test_move_path_renames_and_creates_parents(connect, workspace):
    (workspace / "a.txt").write_text("A")
    r = await call(connect, "move_path", {"src": "a.txt", "dst": "sub/dir/b.txt"})
    assert not r.is_error
    assert text(r) == "moved a.txt -> sub/dir/b.txt"
    assert not (workspace / "a.txt").exists()
    assert (workspace / "sub/dir/b.txt").read_text() == "A"


async def test_move_path_overwrite(connect, workspace):
    (workspace / "a.txt").write_text("A")
    (workspace / "b.txt").write_text("B")
    r = await call(connect, "move_path", {"src": "a.txt", "dst": "b.txt"})
    assert r.is_error and "overwrite=true" in text(r)
    assert (workspace / "a.txt").exists()
    r = await call(connect, "move_path", {"src": "a.txt", "dst": "b.txt", "overwrite": True})
    assert not r.is_error
    assert (workspace / "b.txt").read_text() == "A"


async def test_move_path_refuses_into_itself_and_root(connect, workspace):
    (workspace / "d/inner").mkdir(parents=True)
    r = await call(connect, "move_path", {"src": "d", "dst": "d/inner/d"})
    assert r.is_error and "inside itself" in text(r)
    r = await call(connect, "move_path", {"src": "d/inner", "dst": "d", "overwrite": True})
    assert r.is_error and "destroy the source" in text(r)
    assert (workspace / "d/inner").is_dir()
    r = await call(connect, "move_path", {"src": ".", "dst": "elsewhere"})
    assert r.is_error and "workspace root" in text(r)
    r = await call(connect, "move_path", {"src": "missing", "dst": "x"})
    assert r.is_error and "does not exist" in text(r)


async def test_move_path_moves_symlink_itself(connect, workspace, outside):
    (workspace / "out").symlink_to(outside)
    r = await call(connect, "move_path", {"src": "out", "dst": "renamed"})
    assert not r.is_error, text(r)
    assert (workspace / "renamed").is_symlink()
    assert outside.read_text() == "top secret\n"


@pytest.mark.bwrap
async def test_move_path_path_escape(connect, workspace, outside):
    (workspace / "f.txt").write_text("x")
    for bad in ("../moved.txt", str(outside.parent / "moved.txt")):
        r = await call(connect, "move_path", {"src": "f.txt", "dst": bad})
        assert r.is_error and "outside the workspace" in text(r)
    r = await call(connect, "move_path", {"src": "../secret.txt", "dst": "stolen.txt"})
    assert r.is_error and "outside the workspace" in text(r)
    (workspace / "dirlink").symlink_to(outside.parent)
    r = await call(connect, "move_path", {"src": "f.txt", "dst": "dirlink/moved.txt"})
    assert r.is_error and "outside the workspace" in text(r)
    assert (workspace / "f.txt").exists() and outside.exists()


# --- copy_path -------------------------------------------------------------


async def test_copy_path_file_and_tree(connect, workspace):
    (workspace / "a.txt").write_text("A")
    os.chmod(workspace / "a.txt", 0o640)
    r = await call(connect, "copy_path", {"src": "a.txt", "dst": "copies/a.txt"})
    assert text(r) == "copied a.txt -> copies/a.txt"
    assert (workspace / "copies/a.txt").read_text() == "A"
    assert (workspace / "copies/a.txt").stat().st_mode & 0o777 == 0o640
    (workspace / "d/sub").mkdir(parents=True)
    (workspace / "d/sub/f").write_text("F")
    r = await call(connect, "copy_path", {"src": "d", "dst": "d2"})
    assert not r.is_error
    assert (workspace / "d2/sub/f").read_text() == "F"
    assert (workspace / "d/sub/f").exists()


async def test_copy_path_overwrite_and_self(connect, workspace):
    (workspace / "a.txt").write_text("A")
    (workspace / "b.txt").write_text("B")
    r = await call(connect, "copy_path", {"src": "a.txt", "dst": "b.txt"})
    assert r.is_error and "overwrite=true" in text(r)
    r = await call(connect, "copy_path", {"src": "a.txt", "dst": "b.txt", "overwrite": True})
    assert (workspace / "b.txt").read_text() == "A"
    r = await call(connect, "copy_path", {"src": "a.txt", "dst": "a.txt", "overwrite": True})
    assert r.is_error and "same path" in text(r)
    assert (workspace / "a.txt").read_text() == "A"
    (workspace / "d").mkdir()
    r = await call(connect, "copy_path", {"src": "d", "dst": "d/inside"})
    assert r.is_error and "inside itself" in text(r)


async def test_copy_path_keeps_inner_symlinks_as_links(connect, workspace, outside):
    (workspace / "d").mkdir()
    (workspace / "d/out").symlink_to(outside)
    r = await call(connect, "copy_path", {"src": "d", "dst": "d2"})
    assert not r.is_error, text(r)
    assert (workspace / "d2/out").is_symlink()


@pytest.mark.bwrap
async def test_copy_path_path_escape(connect, workspace, outside):
    await assert_escape_rejected(connect, workspace, outside, "copy_path", lambda p: {"src": p, "dst": "stolen"})
    assert not (workspace / "stolen").exists()
    (workspace / "f.txt").write_text("x")
    r = await call(connect, "copy_path", {"src": "f.txt", "dst": "../copied.txt"})
    assert r.is_error and "outside the workspace" in text(r)
    assert not (outside.parent / "copied.txt").exists()


# --- delete_path -----------------------------------------------------------


async def test_delete_path_file_and_dirs(connect, workspace):
    (workspace / "f.txt").write_text("x")
    (workspace / "empty").mkdir()
    (workspace / "full/sub").mkdir(parents=True)
    assert text(await call(connect, "delete_path", {"path": "f.txt"})) == "deleted f.txt"
    assert text(await call(connect, "delete_path", {"path": "empty"})) == "deleted empty directory empty/"
    r = await call(connect, "delete_path", {"path": "full"})
    assert r.is_error and "recursive=true" in text(r)
    assert (workspace / "full/sub").is_dir()
    r = await call(connect, "delete_path", {"path": "full", "recursive": True})
    assert not r.is_error
    assert list(workspace.iterdir()) == []


async def test_delete_path_refuses_root(connect, workspace):
    (workspace / "keep").write_text("")
    for root in (".", "", str(workspace), "sub/..", f"{workspace}/"):
        r = await call(connect, "delete_path", {"path": root, "recursive": True})
        assert r.is_error and "workspace root" in text(r), (root, text(r))
    assert (workspace / "keep").exists()


async def test_delete_path_removes_link_not_target(connect, workspace, outside):
    (workspace / "out").symlink_to(outside)
    (workspace / "dirlink").symlink_to(outside.parent)
    assert text(await call(connect, "delete_path", {"path": "out"})) == "deleted out"
    r = await call(connect, "delete_path", {"path": "dirlink", "recursive": True})
    assert not r.is_error
    assert outside.read_text() == "top secret\n"
    assert not (workspace / "dirlink").exists()


@pytest.mark.bwrap
async def test_delete_path_missing_and_escape(connect, workspace, outside):
    r = await call(connect, "delete_path", {"path": "missing"})
    assert r.is_error and "does not exist" in text(r)
    (workspace / "dirlink").symlink_to(outside.parent)
    for bad in ("../secret.txt", str(outside), "dirlink/secret.txt"):
        r = await call(connect, "delete_path", {"path": bad})
        assert r.is_error and "outside the workspace" in text(r), (bad, text(r))
    assert outside.exists()


async def test_tool_names_are_registered(connect):
    async with connect("files") as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {
        "read_file",
        "read_many_files",
        "write_file",
        "edit_file",
        "multi_edit",
        "apply_patch",
        "list_dir",
        "tree",
        "file_info",
        "make_dir",
        "move_path",
        "copy_path",
        "delete_path",
    }


# --- regressions -------------------------------------------------------------


async def test_read_file_last_line_without_newline_is_not_marked_truncated(connect, workspace):
    (workspace / "a.txt").write_text("one\ntwo")
    out = text(await call(connect, "read_file", {"path": "a.txt"}))
    assert out.endswith("     2\ttwo")
    assert "truncated" not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
async def test_os_errors_reach_the_agent_with_reason_and_path(connect, workspace):
    locked = workspace / "locked"
    locked.mkdir()
    (locked / "f.txt").write_text("x\n")
    unreadable = workspace / "secret.txt"
    unreadable.write_text("x\n")
    locked.chmod(0o500)
    unreadable.chmod(0)
    try:
        r = await call(connect, "delete_path", {"path": "locked/f.txt"})
        assert r.is_error and "Permission denied" in text(r) and "locked/f.txt" in text(r), text(r)
        r = await call(connect, "read_file", {"path": "secret.txt"})
        assert r.is_error and "Permission denied: secret.txt" in text(r), text(r)
        r = await call(connect, "read_many_files", {"paths": ["secret.txt", "locked/f.txt"]})
        assert not r.is_error
        assert "error: Permission denied" in text(r) and "     1\tx" in text(r)
    finally:
        locked.chmod(0o700)
        unreadable.chmod(0o600)


async def test_parallel_edits_on_one_file_are_not_lost(connect, workspace):
    target = workspace / "many.txt"
    target.write_text("".join(f"line{i}\n" for i in range(100)))
    async with connect("files") as client:
        results = await asyncio.gather(
            *(
                client.call_tool("edit_file", {"path": "many.txt", "old_string": f"line{i}\n", "new_string": f"L{i}\n"})
                for i in range(100)
            )
        )
    assert not any(r.is_error for r in results)
    assert target.read_text() == "".join(f"L{i}\n" for i in range(100))


async def test_tree_does_not_descend_into_dev_link_worktrees(connect, workspace):
    (workspace / ".dev-link" / "worktrees" / "wt1" / "src").mkdir(parents=True)
    (workspace / ".dev-link" / "artifacts").mkdir()
    out = text(await call(connect, "tree", {}))
    assert "worktrees/ (skipped)" in out
    assert "artifacts/" in out
    assert "wt1" not in out


async def test_delete_and_move_symlink_that_points_at_the_root(connect, workspace):
    (workspace / "keep").write_text("")
    (workspace / "rootlink").symlink_to(workspace)
    r = await call(connect, "move_path", {"src": "rootlink", "dst": "renamed"})
    assert not r.is_error, text(r)
    assert text(await call(connect, "delete_path", {"path": "renamed"})) == "deleted renamed"
    assert (workspace / "keep").exists()
    assert not (workspace / "renamed").is_symlink()


async def test_entry_paths_reject_nul_and_unknown_user_cleanly(connect, workspace):
    r = await call(connect, "delete_path", {"path": "a\0b"})
    assert r.is_error and "NUL byte" in text(r)
    r = await call(connect, "delete_path", {"path": "~no_such_user_xyz/a"})
    assert r.is_error and "does not exist" in text(r), text(r)


def test_patch_paths_splits_lines_like_git():
    patch = "--- a/ok.txt\x0b\n+++ b/../evil.txt\x0b\n@@ -0,0 +1 @@\n+x\n"
    assert patch_paths(patch, 1) == ["ok.txt", "../evil.txt"]
