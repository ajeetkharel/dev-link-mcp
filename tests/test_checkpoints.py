import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from conftest import text


async def create(client, message: str = "") -> dict:
    result = await client.call_tool("checkpoint_create", {"message": message})
    assert not result.is_error, text(result)
    return result.structured_content


async def test_create_and_list(connect, workspace, rt):
    (workspace / "a.txt").write_text("one\n")
    async with connect("checkpoints") as client:
        listed = await client.call_tool("checkpoint_list", {})
        assert listed.structured_content["checkpoints"] == []
        first = await create(client, "first")
        assert first["changes_since_previous"] == "first checkpoint"
        assert first["tracked_files"] == 1
        second = await create(client)
        assert second["changes_since_previous"] == "no changes"
        assert second["message"] == "checkpoint"
        (workspace / "a.txt").write_text("two\n")
        third = await create(client, "third")
        assert "1 file changed" in third["changes_since_previous"]
        listed = (await client.call_tool("checkpoint_list", {})).structured_content["checkpoints"]
        limited = (await client.call_tool("checkpoint_list", {"max_count": 1})).structured_content["checkpoints"]
    assert [c["id"] for c in listed] == [third["id"], second["id"], first["id"]]
    assert [c["message"] for c in listed] == ["third", "checkpoint", "first"]
    assert len(limited) == 1
    assert (rt.config.state_dir / "checkpoints.git" / "HEAD").exists()
    assert not (workspace / ".git").exists()


async def test_list_rejects_out_of_range_count(connect):
    async with connect("checkpoints") as client:
        result = await client.call_tool("checkpoint_list", {"max_count": 0})
    assert result.is_error


async def test_excluded_and_gitignored_files_are_not_tracked(connect, workspace):
    (workspace / "keep.py").write_text("x = 1\n")
    for d in ("node_modules", ".venv", "build", "__pycache__"):
        (workspace / d).mkdir()
        (workspace / d / "junk.txt").write_text("junk")
    (workspace / ".gitignore").write_text("secret.env\n")
    (workspace / "secret.env").write_text("TOKEN=1")
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "node_modules" / "new.txt").write_text("more junk")
        diff = await client.call_tool("checkpoint_diff", {"id": cp["id"]})
    assert cp["tracked_files"] == 2  # keep.py and .gitignore
    assert "no differences" in text(diff)


async def test_users_own_git_repo_is_untouched(connect, workspace):
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "f.txt").write_text("hello\n")
    head_before = (workspace / ".git" / "HEAD").read_text()
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "f.txt").write_text("changed\n")
        result = await client.call_tool("checkpoint_restore", {"id": cp["id"]})
    assert not result.is_error, text(result)
    assert cp["tracked_files"] == 1
    assert (workspace / ".git" / "HEAD").read_text() == head_before
    status = subprocess.run(["git", "-C", str(workspace), "log"], capture_output=True, text=True)
    assert status.returncode != 0  # still no commits in the user's repo
    assert (workspace / "f.txt").read_text() == "hello\n"


async def test_diff_shows_modified_added_and_deleted(connect, workspace):
    (workspace / "mod.txt").write_text("old line\n")
    (workspace / "gone.txt").write_text("bye\n")
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "mod.txt").write_text("new line\n")
        (workspace / "gone.txt").unlink()
        (workspace / "fresh.txt").write_text("hi\n")
        full = text(await client.call_tool("checkpoint_diff", {"id": cp["id"]}))
        stat = text(await client.call_tool("checkpoint_diff", {"id": cp["id"], "stat_only": True}))
        scoped = text(await client.call_tool("checkpoint_diff", {"id": cp["id"], "path": "mod.txt"}))
    assert "-old line" in full and "+new line" in full
    assert "fresh.txt" in full and "+hi" in full
    assert "gone.txt" in full and "-bye" in full
    assert "3 files changed" in stat and "+new line" not in stat
    assert "mod.txt" in scoped and "fresh.txt" not in scoped


async def test_diff_without_changes_and_truncation(connect, workspace, rt):
    (workspace / "big.txt").write_text("a\n")
    async with connect("checkpoints") as client:
        cp = await create(client)
        assert "no differences" in text(await client.call_tool("checkpoint_diff", {"id": cp["id"]}))
        (workspace / "big.txt").write_text("".join(f"line {i}\n" for i in range(50_000)))
        rt.config.limits.max_output_bytes = 2000
    # The limit is read at registration time, so reconnect with the new limit.
    async with connect("checkpoints") as client:
        out = text(await client.call_tool("checkpoint_diff", {"id": cp["id"]}))
    assert "diff truncated" in out
    assert len(out) < 3000


@pytest.mark.parametrize("bad_id", ["", "zzzz", "--help", "HEAD", "abc", "deadbeef" * 6])
async def test_invalid_ids_rejected(connect, workspace, bad_id):
    (workspace / "a.txt").write_text("a")
    async with connect("checkpoints") as client:
        await create(client)
        diff = await client.call_tool("checkpoint_diff", {"id": bad_id})
        restore = await client.call_tool("checkpoint_restore", {"id": bad_id})
    assert diff.is_error and "checkpoint id" in text(diff)
    assert restore.is_error and "checkpoint id" in text(restore)


async def test_unknown_id_rejected(connect, workspace):
    async with connect("checkpoints") as client:
        await create(client)
        result = await client.call_tool("checkpoint_diff", {"id": "0123456789abcdef"})
    assert result.is_error and "unknown checkpoint id" in text(result)


async def test_restore_whole_workspace(connect, workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print('v1')\n")
    (workspace / "gone.txt").write_text("keep me\n")
    (workspace / ".gitignore").write_text("*.log\n")
    async with connect("checkpoints") as client:
        cp = await create(client, "v1")
        (workspace / "src" / "app.py").write_text("print('v2')\n")
        (workspace / "gone.txt").unlink()
        (workspace / "new").mkdir()
        (workspace / "new" / "deep").mkdir()
        (workspace / "new" / "deep" / "file.txt").write_text("created later")
        (workspace / "src" / "extra.py").write_text("x")
        (workspace / "run.log").write_text("ignored log")
        (workspace / "node_modules").mkdir()
        (workspace / "node_modules" / "pkg.js").write_text("dep")
        result = await client.call_tool("checkpoint_restore", {"id": cp["id"]})
        assert not result.is_error, text(result)
        data = result.structured_content
        listed = (await client.call_tool("checkpoint_list", {})).structured_content["checkpoints"]
    assert (workspace / "src" / "app.py").read_text() == "print('v1')\n"
    assert (workspace / "gone.txt").read_text() == "keep me\n"
    assert not (workspace / "src" / "extra.py").exists()
    assert not (workspace / "new").exists()  # emptied directories are removed too
    assert (workspace / "src").is_dir()
    assert (workspace / "run.log").read_text() == "ignored log"
    assert (workspace / "node_modules" / "pkg.js").read_text() == "dep"
    assert sorted(data["deleted_files"]) == ["new/deep/file.txt", "src/extra.py"]
    assert data["deleted_count"] == 2
    assert data["restored_files"] == 3
    assert listed[0]["id"] == data["auto_checkpoint"]
    assert listed[0]["message"].startswith("auto: before restore")


async def test_restore_can_be_undone(connect, workspace):
    (workspace / "a.txt").write_text("v1")
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "a.txt").write_text("v2")
        (workspace / "b.txt").write_text("new")
        restored = (await client.call_tool("checkpoint_restore", {"id": cp["id"]})).structured_content
        assert (workspace / "a.txt").read_text() == "v1"
        assert not (workspace / "b.txt").exists()
        undo = await client.call_tool("checkpoint_restore", {"id": restored["auto_checkpoint"]})
        assert not undo.is_error, text(undo)
    assert (workspace / "a.txt").read_text() == "v2"
    assert (workspace / "b.txt").read_text() == "new"


async def test_restore_selected_paths_only(connect, workspace):
    (workspace / "lib").mkdir()
    (workspace / "lib" / "x.py").write_text("x1")
    (workspace / "other.py").write_text("o1")
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "lib" / "x.py").write_text("x2")
        (workspace / "lib" / "added.py").write_text("new")
        (workspace / "other.py").write_text("o2")
        (workspace / "unrelated.py").write_text("stay")
        result = await client.call_tool("checkpoint_restore", {"id": cp["id"], "paths": ["lib"]})
        assert not result.is_error, text(result)
    assert (workspace / "lib" / "x.py").read_text() == "x1"
    assert not (workspace / "lib" / "added.py").exists()
    assert (workspace / "other.py").read_text() == "o2"
    assert (workspace / "unrelated.py").read_text() == "stay"
    assert result.structured_content["deleted_files"] == ["lib/added.py"]


async def test_restore_single_deleted_file(connect, workspace):
    (workspace / "a.txt").write_text("a")
    (workspace / "b.txt").write_text("b")
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "a.txt").unlink()
        (workspace / "b.txt").write_text("b2")
        result = await client.call_tool("checkpoint_restore", {"id": cp["id"], "paths": ["a.txt"]})
        assert not result.is_error, text(result)
    assert (workspace / "a.txt").read_text() == "a"
    assert (workspace / "b.txt").read_text() == "b2"


async def test_restore_unknown_path_and_empty_paths(connect, workspace):
    (workspace / "a.txt").write_text("a")
    async with connect("checkpoints") as client:
        cp = await create(client)
        missing = await client.call_tool("checkpoint_restore", {"id": cp["id"], "paths": ["nope"]})
        empty = await client.call_tool("checkpoint_restore", {"id": cp["id"], "paths": []})
    assert missing.is_error and "no files under nope" in text(missing)
    assert empty.is_error and "paths is empty" in text(empty)


async def test_restore_recreates_tracked_symlink_without_following(connect, workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (workspace / "target.txt").write_text("t")
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "link").symlink_to(outside)
        result = await client.call_tool("checkpoint_restore", {"id": cp["id"]})
        assert not result.is_error, text(result)
    assert not (workspace / "link").is_symlink()
    assert outside.read_text() == "outside"


@pytest.mark.parametrize("bad", ["../escape", "/etc", "link_out"])
@pytest.mark.bwrap
async def test_path_escape_rejected(connect, workspace, tmp_path, bad):
    (tmp_path / "secret").mkdir()
    (workspace / "link_out").symlink_to(tmp_path / "secret")
    (workspace / "a.txt").write_text("a")
    async with connect("checkpoints") as client:
        cp = await create(client)
        diff = await client.call_tool("checkpoint_diff", {"id": cp["id"], "path": bad})
        restore = await client.call_tool("checkpoint_restore", {"id": cp["id"], "paths": [bad]})
    assert diff.is_error and "outside the workspace" in text(diff)
    assert restore.is_error and "outside the workspace" in text(restore)


async def test_host_git_config_and_hooks_are_ignored(connect, workspace, monkeypatch, tmp_path):
    # A hostile global config would run hooks or sign commits; the shadow repo must not read it.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook_ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgSign = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong.git"))
    (workspace / "a.txt").write_text("a")
    async with connect("checkpoints") as client:
        cp = await create(client)
    assert cp["tracked_files"] == 1
    assert not marker.exists()
    assert not (tmp_path / "wrong.git").exists()


async def test_concurrent_creates_are_serialised(connect, workspace):
    for i in range(5):
        (workspace / f"f{i}.txt").write_text(str(i))
    async with connect("checkpoints") as client:
        results = await asyncio.gather(*(client.call_tool("checkpoint_create", {"message": f"m{i}"}) for i in range(5)))
        listed = (await client.call_tool("checkpoint_list", {})).structured_content["checkpoints"]
    assert all(not r.is_error for r in results), [text(r) for r in results]
    assert len(listed) == 5


def test_exclude_file_written(rt):
    from dev_link_mcp.tools.checkpoints import _EXCLUDES, _ShadowRepo

    repo = _ShadowRepo(rt)
    asyncio.run(repo.ensure())
    content = Path(repo.git_dir / "info" / "exclude").read_text().split()
    assert content == list(_EXCLUDES)


async def test_nested_repo_without_commits_does_not_block_snapshot(connect, workspace):
    (workspace / "a.txt").write_text("a")
    subprocess.run(["git", "init", "-q", str(workspace / "sub")], check=True)
    (workspace / "sub" / "f.txt").write_text("f")
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "a.txt").write_text("changed")
        diff = await client.call_tool("checkpoint_diff", {"id": cp["id"]})
    assert cp["tracked_files"] == 1
    assert any("sub/" in line for line in cp["not_snapshotted"])
    assert not diff.is_error and "+changed" in text(diff)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode 000 files")
async def test_unreadable_file_is_reported_not_fatal(connect, workspace):
    (workspace / "ok.txt").write_text("ok")
    locked = workspace / "locked.txt"
    locked.write_text("secret")
    locked.chmod(0)
    try:
        async with connect("checkpoints") as client:
            cp = await create(client)
    finally:
        locked.chmod(0o644)
    assert cp["tracked_files"] == 1
    assert any("locked.txt" in line for line in cp["not_snapshotted"])


async def test_long_and_nul_messages(connect, workspace):
    long_message = "x" * 300_000  # over the kernel's 128 KiB per-argument limit
    async with connect("checkpoints") as client:
        cp = await create(client, long_message)
        nul = await client.call_tool("checkpoint_create", {"message": "a\0b"})
        listed = (await client.call_tool("checkpoint_list", {})).structured_content["checkpoints"]
    assert listed[0]["id"] == cp["id"] and listed[0]["message"] == long_message
    assert nul.is_error and "NUL" in text(nul)


async def test_restore_keeps_ignored_dir_that_blocks_a_checkpoint_file(connect, workspace):
    (workspace / "dist").write_text("was a file")
    (workspace / "a.txt").write_text("a1")
    async with connect("checkpoints") as client:
        cp = await create(client)
        (workspace / "dist").unlink()
        (workspace / "dist").mkdir()
        (workspace / "dist" / "bundle.js").write_text("build output")
        (workspace / "a.txt").write_text("a2")
        result = await client.call_tool("checkpoint_restore", {"id": cp["id"]})
    assert not result.is_error, text(result)
    data = result.structured_content
    # dist/ is excluded, so the auto checkpoint has no copy: git must not be allowed to remove it.
    assert (workspace / "dist" / "bundle.js").read_text() == "build output"
    assert data["not_restored"] == ["dist"]
    assert data["restored_files"] == 1
    assert (workspace / "a.txt").read_text() == "a1"


async def test_restore_deletes_non_utf8_filename(connect, workspace):
    (workspace / "a.txt").write_text("a")
    bad = os.fsencode(workspace) + b"/bad\xff.txt"
    async with connect("checkpoints") as client:
        cp = await create(client)
        with open(bad, "w") as f:
            f.write("x")
        (workspace / "ünï.txt").write_text("u")
        diff = text(await client.call_tool("checkpoint_diff", {"id": cp["id"], "stat_only": True}))
        result = await client.call_tool("checkpoint_restore", {"id": cp["id"]})
    assert not result.is_error, text(result)
    assert "bad�.txt" in diff and "ünï.txt" in diff
    assert sorted(result.structured_content["deleted_files"]) == ["bad�.txt", "ünï.txt"]
    assert sorted(os.listdir(workspace)) == ["a.txt"]


async def test_paths_inside_user_git_dir_are_not_restorable(connect, workspace):
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "a.txt").write_text("a")
    config_before = (workspace / ".git" / "config").read_text()
    async with connect("checkpoints") as client:
        cp = await create(client)
        diff = await client.call_tool("checkpoint_diff", {"id": cp["id"], "path": ".git"})
        restore = await client.call_tool("checkpoint_restore", {"id": cp["id"], "paths": [".git/config"]})
    assert "no differences" in text(diff)
    assert restore.is_error and "no files under .git/config" in text(restore)
    assert (workspace / ".git" / "config").read_text() == config_before
