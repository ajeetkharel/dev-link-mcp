"""The bubblewrap sandbox refuses to start when one of its mounts would expose the state dir."""

from pathlib import Path

import pytest

from dev_link_mcp.config import Config
from dev_link_mcp.config.models import default_state_root
from dev_link_mcp.sandbox import Sandbox, SandboxUnavailable
from dev_link_mcp.sandbox.backends.bubblewrap import Bubblewrap


def _config(workspace: Path, state: Path, **sandbox) -> Config:
    return Config(workspace=workspace, state_dir=state, sandbox={"backend": "bubblewrap", **sandbox}).finalize()


def _exposed(config: Config) -> str | None:
    assert config.state_dir is not None
    return Bubblewrap(config, home=config.state_dir / "home", tmp=config.state_dir / "tmp").exposed_state()


def test_state_dir_under_a_ro_path_is_refused(tmp_path, workspace):
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    message = _exposed(_config(workspace, toolchain / "state", ro_paths=[str(toolchain)]))
    assert message is not None
    assert f"state_dir {toolchain / 'state'} is inside sandbox.ro_paths entry {toolchain}" in message
    assert "--state-dir" in message


def test_state_dir_under_an_rw_path_is_refused(tmp_path, workspace):
    shared = tmp_path / "shared"
    shared.mkdir()
    message = _exposed(_config(workspace, shared / "nested" / "state", rw_paths=[str(shared)]))
    assert message is not None and f"is inside sandbox.rw_paths entry {shared}" in message


def test_rw_path_inside_the_state_dir_is_refused(tmp_path, workspace):
    state = tmp_path / "state"
    # Counted before it exists: the sandbox mounts it as soon as it does.
    logs = state / "processes"
    message = _exposed(_config(workspace, state, rw_paths=[str(logs)]))
    assert message is not None and f"sandbox.rw_paths entry {logs} is inside state_dir {state}" in message


def test_ro_path_through_a_symlink_is_resolved(tmp_path, workspace):
    real = tmp_path / "opt"
    real.mkdir()
    link = tmp_path / "opt-link"
    link.symlink_to(real)
    message = _exposed(_config(workspace, real / "state", ro_paths=[str(link)]))
    assert message is not None and f"sandbox.ro_paths entry {real}" in message


def test_state_dir_under_a_system_mount_is_refused(tmp_path, workspace, monkeypatch):
    # Stands in for /opt or /srv, which the sandbox always mounts read-only.
    system = tmp_path / "srv"
    system.mkdir()
    monkeypatch.setattr("dev_link_mcp.sandbox.backends.bubblewrap._SYSTEM_RO", ("/usr", str(system)))
    message = _exposed(_config(workspace, system / "state"))
    assert message is not None and f"is inside built-in system mount {system}" in message


def test_workspace_inside_the_state_dir_is_refused(tmp_path):
    state = tmp_path / "state"
    ws = state / "ws"
    ws.mkdir(parents=True)
    message = _exposed(_config(ws, state))
    assert message is not None and f"workspace {ws} is inside state_dir {state}" in message


def test_private_home_and_tmp_mounts_do_not_expose_the_state_dir(tmp_path, workspace):
    state = tmp_path / "state"
    config = _config(workspace, state, rw_paths=[str(state / "home" / "cache"), str(state / "tmp" / "x")])
    assert _exposed(config) is None


def test_default_state_dir_is_not_exposed(tmp_path, workspace, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    config = Config(workspace=workspace, sandbox={"backend": "bubblewrap"}).finalize()
    assert config.state_dir is not None and config.state_dir.is_relative_to(default_state_root())
    assert _exposed(config) is None


@pytest.mark.parametrize("backend", ["auto", "bubblewrap"])
def test_sandbox_refuses_to_start(tmp_path, workspace, backend):
    shared = tmp_path / "shared"
    shared.mkdir()
    config = _config(workspace, shared / "state", rw_paths=[str(shared)])
    config.sandbox.backend = backend
    with pytest.raises(SandboxUnavailable, match=r"refusing to start the bubblewrap sandbox: state_dir .* is inside"):
        Sandbox(config)


def test_none_backend_does_not_check(tmp_path, workspace):
    shared = tmp_path / "shared"
    shared.mkdir()
    config = _config(workspace, shared / "state", rw_paths=[str(shared)])
    config.sandbox.backend = "none"
    assert Sandbox(config).backend.name == "none"


def _backend(config: Config) -> Bubblewrap:
    assert config.state_dir is not None
    backend = Bubblewrap(config, home=config.state_dir / "home", tmp=config.state_dir / "tmp")
    # wrap() only builds the argv, so these tests do not need bwrap installed.
    backend.bwrap = "bwrap"
    return backend


def _mounted(args: list[str]) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg in ("--bind", "--ro-bind")]


def test_symlink_planted_for_a_missing_ro_path_is_not_mounted(tmp_path, workspace, caplog):
    """An agent can create a configured path inside a writable mount as a symlink to the state dir.

    Startup refuses that config; wrap() skips such a path anyway in case one slips through.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    state = tmp_path / "state"
    backend = _backend(_config(workspace, state, rw_paths=[str(cache)], ro_paths=[str(cache / "tool")]))
    assert backend.exposed_state() is None and backend.replaceable_mount() is not None
    state.mkdir(parents=True, exist_ok=True)
    (cache / "tool").symlink_to(state)
    for _ in range(2):
        mounts = _mounted(backend.wrap(["true"], workspace, {}))
        assert str(state) not in mounts
        assert str(cache) in mounts
    warnings = [r for r in caplog.records if "sandbox.ro_paths entry" in r.getMessage()]
    assert len(warnings) == 1 and f"{state}" in warnings[0].getMessage()
    assert not backend.visible(state / "connection.json")


def test_symlink_planted_for_a_missing_rw_path_is_not_mounted(tmp_path, workspace, caplog):
    state = tmp_path / "state"
    out = workspace / "out"
    backend = _backend(_config(workspace, state, rw_paths=[str(out)]))
    assert backend.exposed_state() is None and backend.replaceable_mount() is not None
    state.mkdir(parents=True, exist_ok=True)
    out.symlink_to(state)
    mounts = _mounted(backend.wrap(["true"], workspace, {}))
    assert str(state) not in mounts and str(workspace) in mounts
    assert any("sandbox.rw_paths entry" in r.getMessage() for r in caplog.records)


def test_symlink_to_a_parent_of_the_state_dir_is_not_mounted(tmp_path, workspace):
    state = tmp_path / "outer" / "state"
    state.mkdir(parents=True)
    out = workspace / "out"
    backend = _backend(_config(workspace, state, rw_paths=[str(out)]))
    out.symlink_to(tmp_path / "outer")
    assert str(tmp_path / "outer") not in _mounted(backend.wrap(["true"], workspace, {}))


def test_paths_into_the_private_home_stay_mounted(tmp_path, workspace):
    state = tmp_path / "state"
    cache = state / "home" / "cache"
    cache.mkdir(parents=True)
    out = workspace / "out"
    backend = _backend(_config(workspace, state, rw_paths=[str(out)]))
    out.symlink_to(cache)
    assert str(cache) in _mounted(backend.wrap(["true"], workspace, {}))


def test_ro_path_inside_an_rw_path_is_refused(tmp_path, workspace):
    cache = tmp_path / "cache"
    tool = cache / "tool"
    message = _backend(_config(workspace, tmp_path / "state", rw_paths=[str(cache)], ro_paths=[str(tool)]))
    message = message.replaceable_mount()
    assert message is not None
    assert f"sandbox.ro_paths entry {tool} is inside the writable mount sandbox.rw_paths entry {cache}" in message
    assert "replace it with a symlink" in message


def test_rw_path_inside_the_workspace_is_refused(tmp_path, workspace):
    out = workspace / "out"
    message = _backend(_config(workspace, tmp_path / "state", rw_paths=[str(out)])).replaceable_mount()
    assert message is not None and f"sandbox.rw_paths entry {out} is inside the writable mount workspace" in message


def test_path_equal_to_a_writable_mount_is_refused(tmp_path, workspace):
    message = _backend(_config(workspace, tmp_path / "state", ro_paths=[str(workspace)])).replaceable_mount()
    assert message is not None and "is inside the writable mount workspace" in message


def test_path_inside_the_private_home_is_refused(tmp_path, workspace):
    """The agent writes its private home too, so a mount source there can be swapped as well."""
    state = tmp_path / "state"
    cache = state / "home" / "cache"
    message = _backend(_config(workspace, state, rw_paths=[str(cache)])).replaceable_mount()
    assert message is not None and "is inside the writable mount private home" in message


def test_writable_mount_check_resolves_symlinks(tmp_path, workspace):
    link = tmp_path / "ws-link"
    link.symlink_to(workspace)
    message = _backend(_config(workspace, tmp_path / "state", ro_paths=[str(link / "tool")])).replaceable_mount()
    assert message is not None and f"workspace {workspace}" in message


def test_separate_paths_are_allowed(tmp_path, workspace):
    config = _config(
        workspace, tmp_path / "state", rw_paths=[str(tmp_path / "a")], ro_paths=[str(tmp_path / "b"), "~/.nvm"]
    )
    assert _backend(config).replaceable_mount() is None


def test_default_paths_are_allowed(tmp_path, workspace, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    config = Config(workspace=workspace, sandbox={"backend": "bubblewrap"}).finalize()
    assert _backend(config).replaceable_mount() is None


def test_sandbox_refuses_to_start_with_a_replaceable_mount(tmp_path, workspace):
    config = _config(workspace, tmp_path / "state", rw_paths=[str(workspace / "out")])
    with pytest.raises(SandboxUnavailable, match=r"refusing to start the bubblewrap sandbox: sandbox.rw_paths entry"):
        Sandbox(config)
