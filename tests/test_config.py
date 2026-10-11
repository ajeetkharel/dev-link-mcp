import logging
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from dev_link_mcp.cli.common import config_from_args
from dev_link_mcp.cli.main import parse_args
from dev_link_mcp.config import Config, ServerConfig, TunnelConfig, default_user_file, load_config

REPO = Path(__file__).resolve().parents[1]


def _load(config_file: Path | None, cli: dict, **kwargs):
    kwargs.setdefault("environ", {})
    kwargs.setdefault("user_file", None)
    return load_config(config_file, cli, **kwargs)


def test_finalize_resolves_paths_and_generates_token(tmp_path, workspace):
    cfg = Config(workspace=workspace / ".." / "ws", state_dir=tmp_path / "state").finalize()
    assert cfg.workspace == workspace.resolve()
    assert cfg.state_dir == (tmp_path / "state").resolve()
    assert cfg.state_dir.is_dir()
    assert len(cfg.server.token) >= 16
    assert cfg.mcp_path == f"/mcp/{cfg.server.token}"
    assert cfg.ui_path == f"/ui/{cfg.server.token}"


def test_each_finalize_generates_a_fresh_token(tmp_path, workspace):
    first = Config(workspace=workspace, state_dir=tmp_path / "s1").finalize()
    second = Config(workspace=workspace, state_dir=tmp_path / "s2").finalize()
    assert first.server.token != second.server.token


def test_pinned_token_is_kept_and_short_one_rejected(tmp_path, workspace):
    cfg = Config(workspace=workspace, state_dir=tmp_path / "s", server={"token": "x" * 20}).finalize()
    assert cfg.server.token == "x" * 20
    with pytest.raises(ValueError, match="at least 16"):
        Config(workspace=workspace, state_dir=tmp_path / "s", server={"token": "short"}).finalize()


def test_auth_modes_and_paths(tmp_path, workspace):
    bearer = Config(workspace=workspace, state_dir=tmp_path / "s", server={"auth": "bearer"}).finalize()
    assert bearer.server.token and bearer.mcp_path == "/mcp" and bearer.ui_path == f"/ui/{bearer.server.token}"
    none = Config(workspace=workspace, state_dir=tmp_path / "s", server={"auth": "none"}).finalize()
    assert none.server.token == "" and none.mcp_path == "/mcp" and none.ui_path == "/ui"
    with pytest.raises(ValueError, match="Input should be 'path-token', 'bearer' or 'none'"):
        Config(workspace=workspace, state_dir=tmp_path / "s", server={"auth": "basic"})


def test_missing_workspace_rejected(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        Config(workspace=tmp_path / "nope", state_dir=tmp_path / "s").finalize()
    (tmp_path / "file").write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        Config(workspace=tmp_path / "file", state_dir=tmp_path / "s").finalize()


def test_refuses_root_home_and_home_ancestors(tmp_path, monkeypatch):
    home = tmp_path / "users" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for ws in (Path("/"), home, tmp_path / "users"):
        with pytest.raises(ValueError, match="refusing"):
            Config(workspace=ws, state_dir=tmp_path / "state").finalize()
    project = home / "project"
    project.mkdir()
    assert Config(workspace=project, state_dir=tmp_path / "state").finalize().workspace == project


def test_existing_state_dir_is_made_private(tmp_path, workspace):
    state = tmp_path / "shared"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    Config(workspace=workspace, state_dir=state).finalize()
    assert state.stat().st_mode & 0o777 == 0o700


def test_state_dir_inside_workspace_rejected(workspace):
    with pytest.raises(ValueError, match="outside the workspace"):
        Config(workspace=workspace, state_dir=workspace / ".state").finalize()
    with pytest.raises(ValueError, match="outside the workspace"):
        Config(workspace=workspace, state_dir=workspace).finalize()


def test_default_state_dir_uses_xdg_state_home(tmp_path, workspace, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    cfg = Config(workspace=workspace).finalize()
    assert cfg.state_dir.parent == (tmp_path / "xdg" / "dev-link-mcp").resolve()
    assert cfg.state_dir.name.startswith("ws-")
    # Same workspace maps to the same state dir across runs.
    assert Config(workspace=workspace).finalize().state_dir == cfg.state_dir


def test_load_config_yaml_with_overrides(tmp_path, workspace):
    path = tmp_path / "dev-link.yaml"
    path.write_text(
        f"workspace: {workspace}\n"
        f"state-dir: {tmp_path / 'state'}\n"
        "server:\n  port: 9000\n  auth: bearer\n"
        "sandbox:\n  network: false\n  ro_paths: [/opt/tool]\n  env: {FOO: bar}\n"
        "limits:\n  max_processes: 3\n"
    )
    cfg = _load(path, {"server": {"port": 9100, "host": None}, "sandbox": {"backend": "none"}})
    assert cfg.server.port == 9100  # override wins
    assert cfg.server.host == "127.0.0.1"  # None override ignored
    assert cfg.server.auth == "bearer"
    assert cfg.state_dir == (tmp_path / "state").resolve()
    assert cfg.limits.max_processes == 3
    assert cfg.limits.max_output_bytes == 200_000  # untouched default
    # A nested override merges with the file instead of replacing the whole section.
    assert cfg.sandbox.enabled is False
    assert cfg.sandbox.network is False
    assert cfg.sandbox.ro_paths == ["/opt/tool"]
    assert cfg.sandbox.env == {"FOO": "bar"}


def test_load_config_without_file(tmp_path, workspace):
    cfg = _load(None, {"workspace": str(workspace), "state_dir": tmp_path / "s"})
    assert cfg.workspace == workspace


def test_paths_from_yaml_strings(tmp_path, workspace):
    path = tmp_path / "c.yaml"
    path.write_text(f"workspace: {workspace}\nstate_dir: {tmp_path / 's'}\n")
    cfg = _load(path, {})
    assert isinstance(cfg.workspace, Path) and cfg.workspace == workspace
    assert isinstance(cfg.state_dir, Path) and cfg.state_dir == (tmp_path / "s").resolve()


def test_load_config_errors(tmp_path, workspace):
    with pytest.raises(ValueError, match="workspace is required"):
        _load(None, {})
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="must contain a mapping"):
        _load(bad, {"workspace": str(workspace)})
    bad.write_text("server: [unclosed\n")
    with pytest.raises(ValueError, match="is not valid YAML"):
        _load(bad, {"workspace": str(workspace)})
    for body, match in (
        ("colour: blue\n", "unknown config key: colour"),
        ("sandbox:\n  bogus: 1\n", r"unknown config key: sandbox\.bogus"),
        ("sandbox: true\n", "sandbox: must be a mapping"),
        # Settings live in sections; the old flat keys are not accepted.
        ("port: 1\n", "unknown config key: port"),
        ("sandbox:\n  enabled: false\n", r"unknown config key: sandbox\.enabled"),
        ("limits:\n  audit_result_bytes: 1\n", r"unknown config key: limits\.audit_result_bytes"),
        # Methods and properties are not config keys.
        ("finalize: 1\n", "unknown config key: finalize"),
        ("mcp_path: /x\n", "unknown config key: mcp_path"),
    ):
        bad.write_text(body)
        with pytest.raises(ValueError, match=match) as info:
            _load(bad, {"workspace": str(workspace), "state_dir": tmp_path / "s"})
        assert f"[from config file {bad}]" in str(info.value)


def test_missing_config_file_is_an_error(tmp_path, workspace):
    with pytest.raises(ValueError, match="config file not found"):
        _load(tmp_path / "nope.yaml", {"workspace": str(workspace)})


def test_missing_user_file_is_skipped(tmp_path, workspace):
    cfg = _load(None, {"workspace": str(workspace), "state_dir": tmp_path / "s"}, user_file=tmp_path / "nope.yaml")
    assert cfg.server.port == 8765


def test_empty_yaml_file_is_allowed(tmp_path, workspace):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert _load(path, {"workspace": str(workspace), "state_dir": tmp_path / "s"}).workspace == workspace


def test_base_url():
    assert Config(workspace=Path("."), server={"host": "0.0.0.0", "port": 1}).base_url() == "http://127.0.0.1:1"
    assert Config(workspace=Path("."), server={"host": "::1", "port": 2}).base_url() == "http://[::1]:2"
    assert Config(workspace=Path("."), server={"host": "10.0.0.5", "port": 3}).base_url() == "http://10.0.0.5:3"


def test_config_from_args(tmp_path, workspace, monkeypatch):
    args = parse_args(
        [
            "serve",
            str(workspace),
            "--state-dir",
            str(tmp_path / "s"),
            "--auth",
            "bearer",
            "--no-network",
            "--enable",
            "docker",
            "--disable",
            "http",
            "--port",
            "9999",
        ]
    )
    cfg = config_from_args(args)
    assert cfg.server.auth == "bearer" and cfg.server.port == 9999
    assert cfg.sandbox.network is False and cfg.sandbox.enabled is True
    assert cfg.tools.enabled == ["docker"] and cfg.tools.disabled == ["http"]
    cfg = config_from_args(parse_args(["serve", str(workspace), "--state-dir", str(tmp_path / "s"), "--no-sandbox"]))
    assert cfg.sandbox.backend == "none" and cfg.sandbox.enabled is False

    monkeypatch.setenv("DEV_LINK_SERVER__TOKEN", "env-token-0123456789")
    cfg = config_from_args(parse_args(["serve", str(workspace), "--state-dir", str(tmp_path / "s")]))
    assert cfg.server.token == "env-token-0123456789"
    cfg = config_from_args(
        parse_args(["serve", str(workspace), "--state-dir", str(tmp_path / "s"), "--token", "t" * 16])
    )
    assert cfg.server.token == "t" * 16


def test_cli_reads_the_default_user_file(tmp_path, workspace):
    user_file = default_user_file()
    assert user_file == tmp_path / "xdg-config" / "dev-link" / "config.yaml"  # conftest points XDG here
    user_file.parent.mkdir(parents=True)
    user_file.write_text("server:\n  port: 9555\n")
    cfg = config_from_args(parse_args(["serve", str(workspace), "--state-dir", str(tmp_path / "s")]))
    assert cfg.server.port == 9555
    assert cfg.sources["server.port"] == f"user file {user_file}"


def test_auth_none_only_on_loopback(tmp_path, workspace):
    base = ["serve", str(workspace), "--state-dir", str(tmp_path / "s"), "--auth", "none"]
    for host in ("0.0.0.0", "192.168.1.10", "::"):
        with pytest.raises(ValueError, match="loopback"):
            config_from_args(parse_args([*base, "--host", host]))
    for host in ("127.0.0.1", "localhost", "::1"):
        assert config_from_args(parse_args([*base, "--host", host])).server.auth == "none"


@pytest.mark.parametrize("extra", [["--public-url", "https://x.trycloudflare.com"], ["--allowed-host", "x.example"]])
def test_auth_none_refused_behind_tunnel(tmp_path, workspace, extra):
    base = ["serve", str(workspace), "--state-dir", str(tmp_path / "s")]
    with pytest.raises(ValueError, match="path-token"):
        config_from_args(parse_args([*base, "--auth", "none", *extra]))
    # The safe way to expose it: a fresh secret token in the URL path on every start.
    first = config_from_args(parse_args([*base, *extra]))
    second = config_from_args(parse_args([*base, *extra]))
    assert first.server.auth == "path-token"
    assert first.server.token != second.server.token
    assert first.mcp_path == f"/mcp/{first.server.token}"


def test_auth_none_rules_hold_however_the_config_is_built(tmp_path, workspace):
    with pytest.raises(ValidationError, match="loopback"):
        ServerConfig(auth="none", host="0.0.0.0")
    for extra in ({"public_url": "https://x.trycloudflare.com"}, {"allowed_hosts": ["x.example"]}):
        with pytest.raises(ValidationError, match="path-token"):
            ServerConfig(auth="none", **extra)
    path = tmp_path / "c.yaml"
    path.write_text("server:\n  auth: none\n  public_url: https://x.trycloudflare.com\n")
    with pytest.raises(ValueError, match="path-token"):
        _load(path, {"workspace": str(workspace), "state_dir": tmp_path / "s"})
    cfg = Config(workspace=workspace, state_dir=tmp_path / "s", server={"auth": "none"})
    with pytest.raises(ValidationError, match="path-token"):
        cfg.server.allowed_hosts = ["x.example"]
    cfg = Config(workspace=workspace, state_dir=tmp_path / "s", server={"auth": "none"})
    with pytest.raises(ValidationError, match="loopback"):
        cfg.server.host = "0.0.0.0"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("server:\n  port: abc\n", r"server\.port: Input should be a valid integer \(got 'abc'\)"),
        ("server:\n  port: true\n", r"server\.port: Input should be a valid integer \(got True\)"),
        ("server:\n  port: 70000\n", r"server\.port: Input should be less than or equal to 65535"),
        ("server:\n  token: 12345678901234567890\n", r"server\.token: Input should be a valid string"),
        ("server:\n  auth: basic\n", r"server\.auth: Input should be 'path-token', 'bearer' or 'none'"),
        ("server:\n  allowed_hosts: tunnel.example\n", r"server\.allowed_hosts: Input should be a valid list"),
        ("tools:\n  disabled: [http, 3]\n", r"tools\.disabled\.1: Input should be a valid string \(got 3\)"),
        ("sandbox:\n  network: 'false'\n", r"sandbox\.network: Input should be a valid boolean \(got 'false'\)"),
        ("sandbox:\n  backend: docker\n", r"sandbox\.backend: Input should be 'auto', 'bubblewrap' or 'none'"),
        ("sandbox:\n  env: {PORT: 3000}\n", r"sandbox\.env\.PORT: Input should be a valid string \(got 3000\)"),
        ("limits:\n  max_output_bytes: big\n", r"limits\.max_output_bytes: Input should be a valid integer"),
        ("limits:\n  default_timeout_s: '30'\n", r"limits\.default_timeout_s: Input should be a valid number"),
        ("audit:\n  result_bytes: 1.5\n", r"audit\.result_bytes: Input should be a valid integer"),
        ("log_level: verbose\n", r"log_level: Input should be 'DEBUG', 'INFO', 'WARNING' or 'ERROR'"),
    ],
)
def test_load_config_rejects_wrong_types(tmp_path, workspace, body, match):
    path = tmp_path / "c.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match=match) as info:
        _load(path, {"workspace": str(workspace), "state_dir": tmp_path / "s"})
    assert str(info.value).endswith(f"[from config file {path}]")


def test_load_config_rejects_non_path_state_dir(tmp_path, workspace):
    path = tmp_path / "c.yaml"
    path.write_text("state_dir: 5\n")
    with pytest.raises(ValueError, match=r"state_dir: Input should be a valid path \(got 5\)"):
        _load(path, {"workspace": str(workspace)})


def test_load_config_accepts_int_for_float_fields(tmp_path, workspace):
    path = tmp_path / "c.yaml"
    path.write_text("limits:\n  default_timeout_s: 30\n")
    cfg = _load(path, {"workspace": str(workspace), "state_dir": tmp_path / "s"})
    assert cfg.limits.default_timeout_s == 30.0 and isinstance(cfg.limits.default_timeout_s, float)


def test_log_level_accepts_lower_case(tmp_path, workspace):
    cfg = _load(None, {"workspace": str(workspace), "state_dir": tmp_path / "s", "log_level": "debug"})
    assert cfg.log_level == "DEBUG"


def test_token_must_be_url_safe(tmp_path, workspace):
    for bad in ("abc#defghijklmnopq", "a b c d e f g h i j", "abcdefghijklmnop/q", "abcdefghijklmnopé"):
        with pytest.raises(ValueError, match="token may only contain"):
            Config(workspace=workspace, state_dir=tmp_path / "s", server={"token": bad}).finalize()
    ok = "Az09-_.~" * 3
    cfg = Config(workspace=workspace, state_dir=tmp_path / "s", server={"token": ok}).finalize()
    assert cfg.mcp_path == f"/mcp/{ok}"
    # auth "none" ignores the token entirely.
    cfg = Config(workspace=workspace, state_dir=tmp_path / "s", server={"auth": "none", "token": "x y"}).finalize()
    assert cfg.mcp_path == "/mcp"


def test_auth_none_drops_a_configured_token(tmp_path, workspace):
    # A leftover token is not a secret without auth; kept, every log would replace each "e" with ***.
    cfg = Config(workspace=workspace, state_dir=tmp_path / "s", server={"auth": "none", "token": "e"}).finalize()
    assert cfg.server.token == ""
    assert cfg.ui_path == "/ui" and cfg.mcp_path == "/mcp"


def test_assignment_is_validated(config):
    with pytest.raises(ValidationError, match="valid boolean"):
        config.sandbox.network = "no"
    with pytest.raises(ValidationError, match="valid integer"):
        config.limits.max_output_bytes = "big"
    assert config.sandbox.network is True
    assert config.limits.max_output_bytes == 200_000


# --- layers and provenance --------------------------------------------------------------


@pytest.fixture
def layers(tmp_path, workspace):
    """A user file, a --config file, environment and CLI values that all set server.port."""
    user = tmp_path / "user.yaml"
    user.write_text("server:\n  port: 9001\nlimits:\n  max_processes: 5\nlog_level: DEBUG\n")
    config_file = tmp_path / "c.yaml"
    config_file.write_text("server:\n  port: 9002\nlimits:\n  max_processes: 6\n")
    environ = {"DEV_LINK_SERVER__PORT": "9003"}
    cli = {"workspace": str(workspace), "state_dir": tmp_path / "s", "server": {"port": 9004}}
    return user, config_file, environ, cli


def test_each_layer_overrides_the_one_below(layers):
    user, config_file, environ, cli = layers
    base = {"workspace": cli["workspace"], "state_dir": cli["state_dir"]}
    assert _load(None, base).server.port == 8765
    assert _load(None, base, user_file=user).server.port == 9001
    assert _load(config_file, base, user_file=user).server.port == 9002
    assert _load(config_file, base, user_file=user, environ=environ).server.port == 9003
    assert _load(config_file, cli, user_file=user, environ=environ).server.port == 9004


def test_sources_name_the_layer_of_each_value(layers):
    user, config_file, environ, cli = layers
    cfg = _load(config_file, {**cli, "tools": {"enabled": ["docker"]}}, user_file=user, environ=environ)
    assert cfg.sources["server.port"] == "cli"
    assert cfg.sources["workspace"] == "cli"
    assert cfg.sources["limits.max_processes"] == f"config file {config_file}"
    assert cfg.sources["log_level"] == f"user file {user}"
    assert cfg.sources["tools.enabled"] == "cli --enable"
    assert cfg.sources["sandbox.network"] == "default"
    cfg = _load(config_file, {"workspace": cli["workspace"], "state_dir": cli["state_dir"]}, environ=environ)
    assert cfg.sources["server.port"] == "env DEV_LINK_SERVER__PORT"
    # Every setting has a source, so the full picture can be printed.
    assert {"server.token", "sandbox.env", "audit.result_bytes", "tools.disabled", "state_dir"} <= set(cfg.sources)


def test_lists_are_replaced_but_repeatable_flags_append(tmp_path, workspace):
    user = tmp_path / "user.yaml"
    user.write_text("server:\n  allowed_hosts: [a.example]\nsandbox:\n  env: {A: '1', B: '2'}\n")
    config_file = tmp_path / "c.yaml"
    config_file.write_text("server:\n  allowed_hosts: [b.example]\nsandbox:\n  env: {C: '3'}\n")
    environ = {"DEV_LINK_TOOLS__DISABLED": "[http]"}
    cli = {
        "workspace": str(workspace),
        "state_dir": tmp_path / "s",
        "server": {"allowed_hosts": ["c.example"]},
        "tools": {"enabled": ["docker"], "disabled": ["browser"]},
    }
    cfg = _load(config_file, cli, user_file=user, environ=environ)
    assert cfg.server.allowed_hosts == ["b.example", "c.example"]
    assert cfg.sandbox.env == {"C": "3"}  # a mapping is one value, replaced whole
    assert cfg.tools.enabled == ["docker"]
    assert cfg.tools.disabled == ["http", "browser"]
    assert cfg.sources["server.allowed_hosts"] == f"config file {config_file} + cli --allowed-host"
    assert cfg.sources["tools.disabled"] == "env DEV_LINK_TOOLS__DISABLED + cli --disable"
    assert cfg.sources["sandbox.env"] == f"config file {config_file}"
    # Empty repeatable flags leave the configured list alone.
    cfg = _load(config_file, {**cli, "server": {"allowed_hosts": []}}, user_file=user)
    assert cfg.server.allowed_hosts == ["b.example"]
    assert cfg.sources["server.allowed_hosts"] == f"config file {config_file}"


def test_env_values_are_converted_by_field_type(tmp_path, workspace):
    environ = {
        "DEV_LINK_SANDBOX__NETWORK": "false",
        "DEV_LINK_SERVER__PORT": "9200",
        "DEV_LINK_SERVER__HOST": "localhost",
        "DEV_LINK_TOOLS__DISABLED": "[http, browser]",
        "DEV_LINK_SANDBOX__ENV": "{A: b}",
        "DEV_LINK_LIMITS__DEFAULT_TIMEOUT_S": "30",
        "DEV_LINK_LIMITS__MEMORY_MB": "512",
        "DEV_LINK_LIMITS__MAX_PIDS": "null",
        "DEV_LINK_SERVER__TOKEN": "12345678901234567890",
        "DEV_LINK_STATE_DIR": str(tmp_path / "env-state"),
        "DEV_LINK_LOG_LEVEL": "warning",
    }
    cfg = _load(None, {"workspace": str(workspace)}, environ=environ)
    assert cfg.sandbox.network is False
    assert cfg.server.port == 9200
    assert cfg.server.host == "localhost"
    assert cfg.tools.disabled == ["http", "browser"]
    assert cfg.sandbox.env == {"A": "b"}
    assert cfg.limits.default_timeout_s == 30.0
    assert cfg.limits.memory_mb == 512  # an optional int is parsed like an int
    assert cfg.limits.max_pids is None
    assert cfg.server.token == "12345678901234567890"  # a string field keeps the raw text
    assert cfg.state_dir == (tmp_path / "env-state").resolve()
    assert cfg.log_level == "WARNING"


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("DEV_LINK_SANDBOX__NETWORK", "maybe", r"sandbox\.network: Input should be a valid boolean \(got 'maybe'\)"),
        ("DEV_LINK_SERVER__PORT", "80a", r"server\.port: Input should be a valid integer"),
        ("DEV_LINK_SERVER__ALLOWED_HOSTS", "x.example", r"server\.allowed_hosts: Input should be a valid list"),
        ("DEV_LINK_SANDBOX__ENV", "[a", r"sandbox\.env: Input should be a valid dictionary"),
    ],
)
def test_env_values_are_validated(tmp_path, workspace, name, value, match):
    with pytest.raises(ValueError, match=match) as info:
        _load(None, {"workspace": str(workspace), "state_dir": tmp_path / "s"}, environ={name: value})
    assert str(info.value).endswith(f"[from env {name}]")


def test_unknown_env_vars_warn(tmp_path, workspace, caplog):
    environ = {
        "DEV_LINK_SERVER__PORTT": "1",
        "DEV_LINK_COLOUR": "blue",
        "DEV_LINK_TOKEN": "env-token-0123456789",
        "DEV_LINK_SANDBOX": "x",
        "DEV_LINK_SERVER__": "x",
        "OTHER_VAR": "x",
    }
    with caplog.at_level(logging.WARNING):
        cfg = _load(None, {"workspace": str(workspace), "state_dir": tmp_path / "s"}, environ=environ)
    warned = [r.getMessage() for r in caplog.records]
    for name in (
        "DEV_LINK_SERVER__PORTT",
        "DEV_LINK_COLOUR",
        "DEV_LINK_TOKEN",
        "DEV_LINK_SANDBOX",
        "DEV_LINK_SERVER__",
    ):
        assert f"ignoring unknown environment variable {name}" in warned
    assert not any("OTHER_VAR" in m for m in warned)
    assert cfg.server.token != "env-token-0123456789"  # the old alias is gone


def test_dev_link_workspace_env_is_ignored(tmp_path, workspace, caplog):
    other = tmp_path / "other"
    other.mkdir()
    environ = {"DEV_LINK_WORKSPACE": str(other)}
    with caplog.at_level(logging.WARNING):
        cfg = _load(None, {"workspace": str(workspace), "state_dir": tmp_path / "s"}, environ=environ)
        assert cfg.workspace == workspace
        with pytest.raises(ValueError, match="workspace is required"):
            _load(None, {"state_dir": tmp_path / "s"}, environ=environ)
    assert "DEV_LINK_WORKSPACE" not in caplog.text


def test_config_files_in_the_workspace_are_never_read(tmp_path, workspace, monkeypatch):
    # The agent can write anywhere in the workspace and must not reconfigure the next start.
    hostile = "sandbox:\n  backend: none\n"
    names = ("dev-link.yaml", ".dev-link.yaml", "config.yaml", ".dev-link/config.yaml", ".config/dev-link/config.yaml")
    for rel in names:
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text(hostile)
    monkeypatch.chdir(workspace)
    cfg = load_config(None, {"workspace": ".", "state_dir": tmp_path / "s"}, environ={})
    assert cfg.sandbox.backend == "auto" and cfg.sandbox.enabled
    assert cfg.sources["sandbox.backend"] == "default"

    with pytest.raises(ValueError, match="inside the workspace"):
        load_config(workspace / "dev-link.yaml", {"workspace": ".", "state_dir": tmp_path / "s"}, environ={})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(workspace / ".config"))
    with pytest.raises(ValueError, match="inside the workspace"):
        load_config(None, {"workspace": ".", "state_dir": tmp_path / "s"}, environ={})


def test_generated_docs_are_current():
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen_docs.py"), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- tool group settings ----------------------------------------------------------------


def test_group_settings_merge_key_by_key_with_sources(tmp_path, workspace):
    user = tmp_path / "user.yaml"
    user.write_text("groups:\n  browser:\n    headless: false\n    max-pages: 3\n  http:\n    max_redirects: 2\n")
    config_file = tmp_path / "c.yaml"
    config_file.write_text("groups:\n  browser:\n    max_pages: 4\n")
    environ = {"DEV_LINK_GROUPS__HTTP__DEFAULT_TIMEOUT_S": "12.5", "DEV_LINK_GROUPS__MY_PLUGIN__GREETING": "Hi"}
    cli = {"workspace": str(workspace), "state_dir": tmp_path / "s"}
    cfg = _load(config_file, cli, user_file=user, environ=environ)
    assert cfg.groups == {
        "browser": {"headless": False, "max_pages": 4},
        "http": {"max_redirects": 2, "default_timeout_s": 12.5},
        "my_plugin": {"greeting": "Hi"},
    }
    assert cfg.sources["groups.browser.headless"] == f"user file {user}"
    assert cfg.sources["groups.browser.max_pages"] == f"config file {config_file}"
    assert cfg.sources["groups.http.default_timeout_s"] == "env DEV_LINK_GROUPS__HTTP__DEFAULT_TIMEOUT_S"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("groups: [browser]\n", r"groups: must be a mapping \(got \['browser'\]\)"),
        ("groups:\n  browser: true\n", r"groups\.browser: must be a mapping \(got True\)"),
    ],
)
def test_group_settings_must_be_mappings(tmp_path, workspace, body, match):
    path = tmp_path / "c.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match=match) as info:
        _load(path, {"workspace": str(workspace), "state_dir": tmp_path / "s"})
    assert str(info.value).endswith(f"[from config file {path}]")


def test_incomplete_group_env_vars_warn(tmp_path, workspace, caplog):
    environ = {"DEV_LINK_GROUPS__BROWSER": "x", "DEV_LINK_GROUPS__": "x"}
    with caplog.at_level(logging.WARNING):
        cfg = _load(None, {"workspace": str(workspace), "state_dir": tmp_path / "s"}, environ=environ)
    assert cfg.groups == {}
    for name in environ:
        assert f"ignoring unknown environment variable {name}" in caplog.text


def test_audit_retention_days(tmp_path, workspace):
    assert Config(workspace=workspace).audit.retention_days == 30
    path = tmp_path / "c.yaml"
    path.write_text("audit:\n  retention_days: null\n")
    assert _load(path, {"workspace": str(workspace), "state_dir": tmp_path / "s"}).audit.retention_days is None
    for bad in (0, -1):
        path.write_text(f"audit:\n  retention_days: {bad}\n")
        with pytest.raises(ValueError, match=r"audit\.retention_days: Input should be greater than or equal to 1"):
            _load(path, {"workspace": str(workspace), "state_dir": tmp_path / "s"})


@pytest.mark.parametrize(
    "url",
    [
        "http://x.ngrok-free.app",
        "https://x.ngrok-free.app/mcp",
        "https://x.ngrok-free.app?a=1",
        "https://x.ngrok-free.app?",
        "https://x.ngrok-free.app#",
        "https://user:pw@x.ngrok-free.app",
        "https://x.ngrok-free.app:8443",
        "x.ngrok-free.app",
    ],
)
def test_ngrok_url_must_be_a_bare_https_origin(url):
    with pytest.raises(ValidationError, match="ngrok_url"):
        TunnelConfig(ngrok_url=url)


def test_ngrok_url_is_normalised():
    assert TunnelConfig(ngrok_url=" https://x.ngrok-free.app/ ").ngrok_url == "https://x.ngrok-free.app"
    assert TunnelConfig(ngrok_url="HTTPS://Name.ngrok-free.app").ngrok_url == "https://name.ngrok-free.app"
    assert TunnelConfig().ngrok_url == ""
