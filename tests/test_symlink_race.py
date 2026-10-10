"""Host-side file tools must not follow a symlink swapped in after a path was checked.

A sandboxed process sees the workspace at its real host path, so it can replace a workspace
directory with a symlink to a host directory at any moment. These tests make that swap happen
deterministically: just before the server opens its first directory descriptor, or just
before SQLite opens the database, after every path check has already passed.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import text
from mcp.server.mcpserver.exceptions import ToolError

import dev_link_mcp.workspace as workspace_module
from dev_link_mcp.browse import BrowseError, FileBrowser
from dev_link_mcp.tools.checkpoints import _delete_files

SECRET = "HOST SECRET\n"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A host directory next to the workspace, holding files named like the workspace ones."""
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "secret.txt").write_text(SECRET)
    (directory / "data.csv").write_text("secret,column\nHOST,SECRET\n")
    return directory


@pytest.fixture
def tree(workspace: Path, rt: Any) -> Path:
    """`a/` with decoy files and a confined workspace, whatever sandbox this host has."""
    rt.workspace.confined = True
    (workspace / "a").mkdir()
    (workspace / "a" / "secret.txt").write_text("decoy\n")
    (workspace / "a" / "data.csv").write_text("name,value\nx,1\n")
    return workspace


def snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


def swap_dir(workspace: Path, outside: Path) -> Callable[[], None]:
    def swap() -> None:
        (workspace / "a").rename(workspace / "a_real")
        (workspace / "a").symlink_to(outside)

    return swap


def swap_before(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, swap: Callable[[], None]) -> None:
    """Run `swap` once, just before the first call to `owner.name`."""
    original = getattr(owner, name)
    done = False

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal done
        if not done:
            done = True
            swap()
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)


TOOL_CALLS = [
    ("read_file", {"path": "a/secret.txt"}),
    ("read_many_files", {"paths": ["a/secret.txt"]}),
    ("write_file", {"path": "a/secret.txt", "content": "AGENT\n"}),
    ("write_file", {"path": "a/new.txt", "content": "AGENT\n"}),
    ("edit_file", {"path": "a/secret.txt", "old_string": "decoy", "new_string": "AGENT"}),
    ("delete_path", {"path": "a/secret.txt"}),
    ("move_path", {"src": "a/secret.txt", "dst": "moved.txt"}),
    ("move_path", {"src": "b.txt", "dst": "a/b.txt"}),
    ("copy_path", {"src": "b.txt", "dst": "a/b.txt"}),
    ("make_dir", {"path": "a/new/dir"}),
    ("list_dir", {"path": "a"}),
    ("tree", {"path": "a"}),
    ("file_info", {"path": "a/secret.txt"}),
]


@pytest.mark.parametrize(("tool", "args"), TOOL_CALLS, ids=[f"{t}-{i}" for i, (t, _) in enumerate(TOOL_CALLS)])
async def test_files_tools_refuse_a_directory_swapped_for_a_symlink(connect, tree, outside, monkeypatch, tool, args):
    (tree / "b.txt").write_text("b\n")
    before = snapshot(outside)
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    async with connect("files") as client:
        result = await client.call_tool(tool, args)
    out = text(result)
    assert (tree / "a").is_symlink(), "the swap did not happen"
    # read_many_files reports per-file errors inline instead of failing the call.
    assert result.is_error or "error:" in out, out
    assert "replaced by a symlink" in out, out
    assert "HOST" not in out and "secret,column" not in out
    assert snapshot(outside) == before


async def test_a_swapped_leaf_is_handled_as_the_link_itself(connect, tree, outside, monkeypatch):
    # When the swapped directory is the entry being copied or deleted, the server acts on the
    # new symlink without following it: nothing outside is read or removed.
    before = snapshot(outside)
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    async with connect("files") as client:
        copied = await client.call_tool("copy_path", {"src": "a", "dst": "copy"})
        deleted = await client.call_tool("delete_path", {"path": "a", "recursive": True})
    assert not copied.is_error and (tree / "copy").is_symlink(), text(copied)
    assert not deleted.is_error and not (tree / "a").exists(), text(deleted)
    assert snapshot(outside) == before


async def test_replace_in_files_write_refuses_a_swap_after_the_read(connect, tree, outside, monkeypatch):
    (tree / "a" / "secret.txt").unlink()
    (tree / "a" / "data.csv").unlink()
    (tree / "a" / "victim.txt").write_text("MARK agent copy\n")
    (outside / "victim.txt").write_text("MARK host original\n")
    before = snapshot(outside)
    swap_before(monkeypatch, workspace_module.Workspace, "write_file", swap_dir(tree, outside))
    async with connect("search") as client:
        result = await client.call_tool(
            "replace_in_files", {"pattern": "MARK", "replacement": "PWNED", "regex": False, "dry_run": False}
        )
    assert result.is_error and "replaced by a symlink" in text(result), text(result)
    assert snapshot(outside) == before


async def test_replace_in_files_read_refuses_a_swap(connect, tree, outside, monkeypatch):
    (tree / "a" / "data.csv").unlink()
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    async with connect("search") as client:
        result = await client.call_tool("replace_in_files", {"pattern": "SECRET|decoy", "replacement": "x"})
    out = text(result)
    assert "replaced by a symlink" in out, out
    assert "HOST" not in out and (outside / "secret.txt").read_text() == SECRET


async def test_csv_preview_refuses_a_swap(connect, tree, outside, monkeypatch):
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    async with connect("data") as client:
        result = await client.call_tool("csv_preview", {"path": "a/data.csv"})
    assert result.is_error and "replaced by a symlink" in text(result), text(result)
    assert "secret" not in text(result)


@pytest.mark.parametrize("tool", ["sqlite_query", "sqlite_schema"])
async def test_sqlite_refuses_a_database_swapped_before_sqlite_opens_it(connect, tree, outside, monkeypatch, tool):
    for directory, value in ((tree / "a", "decoy"), (outside, "HOST SECRET")):
        conn = sqlite3.connect(directory / "app.db")
        conn.execute(f"create table t_{value.split()[0].lower()}(v text)")
        conn.execute(f"insert into t_{value.split()[0].lower()} values (?)", (value,))
        conn.commit()
        conn.close()
    swap_before(monkeypatch, sqlite3, "connect", swap_dir(tree, outside))
    args: dict[str, Any] = {"db_path": "a/app.db"}
    if tool == "sqlite_query":
        args["sql"] = "select name from sqlite_schema"
    async with connect("data") as client:
        result = await client.call_tool(tool, args)
    assert (tree / "a").is_symlink(), "the swap did not happen"
    assert result.is_error and "changed while it was being opened" in text(result), text(result)
    assert "t_host" not in text(result)


async def test_sqlite_write_creates_a_new_database_through_the_walk(connect, tree, outside, monkeypatch):
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    async with connect("data") as client:
        result = await client.call_tool(
            "sqlite_query", {"db_path": "a/new.db", "sql": "create table t(v)", "write": True}
        )
    assert result.is_error and "replaced by a symlink" in text(result), text(result)
    assert not (outside / "new.db").exists()


async def test_sqlite_still_works_without_a_swap(connect, tree):
    async with connect("data") as client:
        created = await client.call_tool(
            "sqlite_query", {"db_path": "a/new.db", "sql": "create table t(v)", "write": True}
        )
        listed = await client.call_tool("sqlite_schema", {"db_path": "a/new.db"})
    assert not created.is_error, text(created)
    assert "table t" in text(listed), text(listed)


async def test_checkpoint_delete_refuses_a_swap(rt, tree, outside, monkeypatch):
    (outside / "added.txt").write_text(SECRET)
    (tree / "a" / "added.txt").write_text("agent\n")
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    with pytest.raises(ToolError, match="replaced by a symlink"):
        await asyncio.to_thread(_delete_files, rt, ["a/added.txt"])
    assert (outside / "added.txt").read_text() == SECRET


def test_dashboard_read_refuses_a_swapped_directory(tree, outside, monkeypatch):
    browser = FileBrowser(tree, None)
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    with pytest.raises(BrowseError, match="replaced by a symlink") as caught:
        browser.read("a/secret.txt")
    assert caught.value.status == 400


def test_dashboard_listing_refuses_a_swapped_directory(tree, outside, monkeypatch):
    browser = FileBrowser(tree, None)
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    with pytest.raises(BrowseError, match="replaced by a symlink"):
        browser.listing("a")


def test_dashboard_read_refuses_a_fifo_swapped_in_without_blocking(tree, monkeypatch):
    browser = FileBrowser(tree, None)
    target = tree / "a" / "secret.txt"

    def swap() -> None:
        target.unlink()
        os.mkfifo(target)

    swap_before(monkeypatch, workspace_module, "_open_dir", swap)
    # A blocking open of a FIFO with no writer would hang this test forever.
    with pytest.raises(BrowseError, match="not a regular file"):
        browser.read("a/secret.txt")


def test_dashboard_download_refuses_a_swapped_directory(tree, outside, monkeypatch):
    browser = FileBrowser(tree, None)
    swap_before(monkeypatch, workspace_module, "_open_dir", swap_dir(tree, outside))
    with pytest.raises(BrowseError, match="replaced by a symlink") as caught:
        browser.open_download("a/secret.txt")
    assert caught.value.status == 400
