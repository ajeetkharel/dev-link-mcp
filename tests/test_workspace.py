import os
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from dev_link_mcp.workspace import Workspace


@pytest.fixture
def ws(workspace: Path) -> Workspace:
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("x")
    return Workspace(workspace)


def test_relative_and_default(ws, workspace):
    assert ws.resolve() == workspace
    assert ws.resolve("") == workspace
    assert ws.resolve("  ") == workspace
    assert ws.resolve(".") == workspace
    assert ws.resolve("src/a.py") == workspace / "src" / "a.py"
    assert ws.resolve("src/../src/./a.py") == workspace / "src" / "a.py"
    assert ws.resolve(Path("src")) == workspace / "src"


def test_absolute_inside(ws, workspace):
    assert ws.resolve(str(workspace / "src" / "a.py"), must_exist=True) == workspace / "src" / "a.py"
    assert ws.resolve(str(workspace)) == workspace


def test_absolute_outside_rejected(ws, tmp_path):
    for path in ("/etc/passwd", "/", str(tmp_path), str(tmp_path / "ws-other")):
        with pytest.raises(ToolError, match="outside the workspace"):
            ws.resolve(path)


def test_sibling_with_common_prefix_rejected(ws, workspace):
    sibling = workspace.parent / (workspace.name + "2")
    sibling.mkdir()
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.resolve(str(sibling))


def test_dotdot_escape_rejected(ws):
    for path in ("..", "../x", "src/../../x", "src/../../../../etc/passwd"):
        with pytest.raises(ToolError, match="outside the workspace"):
            ws.resolve(path)


def test_symlink_escape_rejected(ws, workspace, tmp_path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("s")
    os.symlink(outside_dir / "secret.txt", workspace / "file-link")
    os.symlink(outside_dir, workspace / "dir-link")
    os.symlink(tmp_path / "missing-target", workspace / "dangling-link")
    for path in ("file-link", "dir-link", "dir-link/secret.txt", "dir-link/new.txt", "dangling-link"):
        with pytest.raises(ToolError, match="outside the workspace"):
            ws.resolve(path)


def test_symlink_inside_is_followed(ws, workspace):
    os.symlink(workspace / "src", workspace / "alias")
    assert ws.resolve("alias/a.py", must_exist=True) == workspace / "src" / "a.py"


def test_workspace_root_behind_symlink(tmp_path, workspace):
    link = tmp_path / "ws-link"
    os.symlink(workspace, link)
    ws = Workspace(link)
    assert ws.root == workspace
    assert ws.resolve(str(link / "x")) == workspace / "x"


def test_nonexistent_and_must_exist(ws, workspace):
    assert ws.resolve("new/dir/file.txt") == workspace / "new" / "dir" / "file.txt"
    with pytest.raises(ToolError, match=r"does not exist: new/file\.txt"):
        ws.resolve("new/file.txt", must_exist=True)


def test_tilde_expands_to_host_home_and_is_rejected(ws):
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.resolve("~")
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.resolve("~/.ssh/id_rsa")


def test_tilde_of_unknown_user_is_a_literal_name(ws, workspace):
    (workspace / "~nosuchuser-dev-link").mkdir()
    assert ws.resolve("~nosuchuser-dev-link") == workspace / "~nosuchuser-dev-link"


def test_null_byte_is_a_tool_error(ws):
    with pytest.raises(ToolError):
        ws.resolve("a\x00b")


def test_rel(ws, workspace, tmp_path):
    assert ws.rel(workspace) == "."
    assert ws.rel(workspace / "src" / "a.py") == "src/a.py"
    assert ws.rel(tmp_path / "elsewhere") == str(tmp_path / "elsewhere")


def test_artifact_path(ws, workspace):
    first = ws.artifact_path("my shot/../x", ".png")
    assert first.parent == workspace / ".dev-link" / "artifacts"
    assert (workspace / ".dev-link" / ".gitignore").read_text() == "*\n"
    assert "/" not in first.name
    first.write_text("x")
    second = ws.artifact_path("my shot/../x", ".png")
    assert second != first and not second.exists()
    assert ws.artifact_path("///", ".txt").name.endswith("-artifact.txt")


def test_dev_link_symlink_is_refused(ws, workspace, tmp_path):
    # The agent can create .dev-link from the shell; the server must not write through it to the host.
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / ".dev-link")
    with pytest.raises(ToolError, match=r"\.dev-link is a symlink"):
        ws.artifact_path("shot", ".png")
    os.unlink(workspace / ".dev-link")
    os.symlink(tmp_path / "missing", workspace / ".dev-link")
    with pytest.raises(ToolError, match="symlink"):
        ws.dev_link_dir  # noqa: B018
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "missing").exists()


def test_dev_link_artifacts_symlink_is_refused(ws, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, ws.dev_link_dir / "artifacts")
    with pytest.raises(ToolError, match=r"\.dev-link/artifacts is a symlink"):
        ws.artifact_path("shot", ".png")


def test_dev_link_as_file_is_a_tool_error(ws, workspace):
    (workspace / ".dev-link").write_text("not a dir")
    with pytest.raises(ToolError, match="not a directory"):
        ws.artifact_path("shot", ".png")


def test_unconfined_allows_dev_link_symlink(workspace, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.symlink(target, workspace / ".dev-link")
    path = Workspace(workspace, confined=False).artifact_path("shot", ".png")
    assert path.parent == workspace / ".dev-link" / "artifacts"


def test_unconfined_accepts_any_host_path(workspace, tmp_path):
    ws = Workspace(workspace, confined=False)
    assert ws.resolve("/etc/hostname") == Path("/etc/hostname").resolve()
    assert ws.resolve("../outside") == (tmp_path / "outside")


def test_descriptor_helpers_refuse_symlinks_below_the_root(ws, workspace, tmp_path):
    # Paths as `resolve` returned them before a sandboxed process swapped `src` for a symlink.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("host")
    checked = ws.resolve("src/a.py")
    (workspace / "src").rename(workspace / "src_real")
    (workspace / "src").symlink_to(outside)
    with pytest.raises(ToolError, match="src is not a directory, or was replaced by a symlink"):
        ws.open_file(checked)
    with pytest.raises(ToolError, match="replaced by a symlink"):
        ws.write_file(checked, b"agent")
    with pytest.raises(ToolError, match="replaced by a symlink"):
        ws.make_dirs(checked.parent / "new")
    with pytest.raises(ToolError, match="replaced by a symlink"), ws.open_dir(checked.parent):
        pass
    assert sorted(p.name for p in outside.iterdir()) == ["a.py"]
    assert (outside / "a.py").read_text() == "host"


def test_open_file_refuses_a_leaf_symlink_and_a_fifo_without_blocking(ws, workspace):
    (workspace / "link.py").symlink_to(workspace / "src" / "a.py")
    with pytest.raises(ToolError, match=r"link\.py was replaced by a symlink"):
        ws.open_file(workspace / "link.py")
    os.mkfifo(workspace / "pipe")
    with pytest.raises(ToolError, match="pipe is not a regular file"):
        ws.open_file(workspace / "pipe")


def test_symlinks_inside_the_workspace_still_work_through_resolve(ws, workspace):
    (workspace / "inner").symlink_to(workspace / "src")
    with ws.open_file(ws.resolve("inner/a.py")) as f:
        assert f.read() == b"x"
    assert ws.write_file(ws.resolve("inner/a.py"), b"y") is True
    assert (workspace / "src" / "a.py").read_text() == "y"


def test_write_file_creates_or_replaces_and_keeps_the_mode(ws, workspace):
    target = workspace / "src" / "a.py"
    target.chmod(0o751)
    assert ws.write_file(target, b"new") is True
    assert target.read_bytes() == b"new" and (target.stat().st_mode & 0o777) == 0o751
    assert ws.write_file(workspace / "src" / "b.py", b"b") is False
    assert (workspace / "src" / "b.py").read_bytes() == b"b"
    assert sorted(p.name for p in (workspace / "src").iterdir()) == ["a.py", "b.py"]
    with pytest.raises(ToolError, match="is a directory"):
        ws.write_file(workspace / "src", b"x")


def test_make_dirs_creates_missing_parents(ws, workspace):
    ws.make_dirs(workspace / "x" / "y" / "z")
    assert (workspace / "x" / "y" / "z").is_dir()
    ws.make_dirs(workspace / "x" / "y" / "z")


def test_unconfined_descriptor_helpers_follow_symlinks(workspace, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "f.txt").write_text("host")
    (workspace / "link").symlink_to(outside)
    ws = Workspace(workspace, confined=False)
    with ws.open_file(workspace / "link" / "f.txt") as f:
        assert f.read() == b"host"


def test_read_at_most_caps_the_actual_read(tmp_path):
    import io

    from dev_link_mcp.workspace import read_at_most

    assert read_at_most(io.BytesIO(b"12345"), 5) == b"12345"
    assert read_at_most(io.BytesIO(b"123456"), 5) is None
