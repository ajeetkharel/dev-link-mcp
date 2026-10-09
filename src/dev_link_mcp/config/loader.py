"""Layered config loading with a record of where every value came from.

Layers, lowest to highest: model defaults, the user file, the --config file, DEV_LINK_*
environment variables, command-line flags. No file inside the workspace is ever read: the
agent can write there and could otherwise turn off the sandbox for the next start.
"""

from __future__ import annotations

import logging
import os
import types
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import yaml
from pydantic import ValidationError

from dev_link_mcp.config.models import Config, Section

log = logging.getLogger(__name__)

ENV_PREFIX = "DEV_LINK_"

# The sandbox exports this to agent commands; it describes a running server, not a setting.
_ENV_IGNORED = frozenset({"DEV_LINK_WORKSPACE"})

# Repeatable flags add to the configured list instead of replacing it.
_CLI_APPEND = {
    ("server", "allowed_hosts"): "--allowed-host",
    ("tools", "enabled"): "--enable",
    ("tools", "disabled"): "--disable",
}

SECTIONS: dict[str, type[Section]] = {
    name: f.annotation
    for name, f in Config.model_fields.items()
    if isinstance(f.annotation, type) and issubclass(f.annotation, Section)
}


def default_user_file() -> Path:
    """The per-user config file: `$XDG_CONFIG_HOME/dev-link/config.yaml` (default `~/.config/...`)."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "dev-link" / "config.yaml"


def load_config(
    config_file: Path | None,
    cli: dict[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    user_file: Path | Literal["default"] | None = "default",
) -> Config:
    """Merge all config layers into a finalized Config.

    `cli` is nested like the config (`{"server": {"port": 9000}}`); None values are skipped and
    the lists for --allowed-host, --enable and --disable are appended to the merged value.
    `environ` defaults to os.environ. `user_file` is read only if it exists; "default" means
    default_user_file() and None skips it. `config_file` must exist.

    The result's `sources` maps each dotted key to the layer that set it. Raises ValueError
    with one line per problem for missing or unreadable files, files inside the workspace,
    unknown keys, wrong types and everything Config.finalize() rejects. Logs a warning for
    each unknown DEV_LINK_* variable.
    """
    if user_file == "default":
        user_file = default_user_file()
    data: dict[str, Any] = {}
    sources: dict[str, str] = {}
    files: list[tuple[Path, str]] = []
    if user_file is not None and user_file.expanduser().is_file():
        files.append((user_file.expanduser(), f"user file {user_file}"))
    if config_file is not None:
        if not config_file.expanduser().is_file():
            raise ValueError(f"config file not found: {config_file}")
        files.append((config_file.expanduser(), f"config file {config_file}"))
    for path, label in files:
        _apply(data, sources, _read_yaml(path, label), label)
    for layer, label in _env_layers(os.environ if environ is None else environ):
        _apply(data, sources, layer, label)
    _apply_cli(data, sources, cli)

    if data.get("workspace") is None:
        raise ValueError("workspace is required (positional argument or 'workspace' in the config file)")
    _refuse_workspace_files(data["workspace"], files)
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ValueError(describe_errors(exc, sources)) from None
    for key in _leaf_keys():
        sources.setdefault(key, "default")
    config._sources = sources
    return config.finalize()


def _read_yaml(path: Path, label: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{label} is not valid YAML: {exc}") from None
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} must contain a mapping")
    return loaded


def _apply(data: dict[str, Any], sources: dict[str, str], layer: Mapping[Any, Any], label: str) -> None:
    """Merge one layer into `data`: sections key by key, every other value replaced whole."""
    for raw_key, value in layer.items():
        key = str(raw_key).replace("-", "_")
        if key == "groups":
            _apply_groups(data.setdefault("groups", {}), sources, value, label)
            continue
        if key not in SECTIONS:
            data[key] = value
            sources[key] = label
            continue
        if not isinstance(value, dict):
            raise ValueError(f"{key}: must be a mapping (got {value!r}) [from {label}]")
        section = data.setdefault(key, {})
        for raw_sub, sub_value in value.items():
            sub = str(raw_sub).replace("-", "_")
            section[sub] = sub_value
            sources[f"{key}.{sub}"] = label


def _apply_groups(groups: dict[str, Any], sources: dict[str, str], value: Any, label: str) -> None:
    """Merge `groups` one level deeper than a section: each group's keys are set one by one."""
    if not isinstance(value, dict):
        raise ValueError(f"groups: must be a mapping (got {value!r}) [from {label}]")
    for raw_group, settings in value.items():
        # Group names are kept as written: plugin names may contain hyphens.
        group = str(raw_group)
        if not isinstance(settings, dict):
            raise ValueError(f"groups.{group}: must be a mapping (got {settings!r}) [from {label}]")
        merged = groups.setdefault(group, {})
        for raw_sub, sub_value in settings.items():
            sub = str(raw_sub).replace("-", "_")
            merged[sub] = sub_value
            sources[f"groups.{group}.{sub}"] = label


def _apply_cli(data: dict[str, Any], sources: dict[str, str], cli: Mapping[str, Any]) -> None:
    plain: dict[str, Any] = {}
    for key, value in cli.items():
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if v is not None}
        if value is not None:
            plain[key] = value
    appends = {(s, k): plain[s].pop(k) for s, k in _CLI_APPEND if k in plain.get(s, {})}
    _apply(data, sources, plain, "cli")
    for (section, key), extra in appends.items():
        if not extra:
            continue
        current = data.setdefault(section, {}).get(key, [])
        dotted, flag = f"{section}.{key}", _CLI_APPEND[(section, key)]
        if not isinstance(current, list):
            continue  # validation reports the lower layer's wrong type
        data[section][key] = [*current, *extra]
        sources[dotted] = f"{sources[dotted]} + cli {flag}" if current else f"cli {flag}"


def _env_layers(environ: Mapping[str, str]) -> Iterator[tuple[dict[str, Any], str]]:
    """One single-value layer per DEV_LINK_* variable, so each keeps its own source label."""
    for name in sorted(environ):
        if not name.startswith(ENV_PREFIX) or name in _ENV_IGNORED:
            continue
        rest = name[len(ENV_PREFIX) :].lower()
        section, nested, key = rest.partition("__")
        if section == "groups":
            # Group settings are checked by the group at start, so every value is parsed as YAML.
            group, _, sub = key.partition("__")
            if not group or not sub:
                log.warning("ignoring unknown environment variable %s", name)
                continue
            yield {"groups": {group: {sub: _yaml_value(environ[name])}}}, f"env {name}"
            continue
        if nested:
            model = SECTIONS.get(section)
            field = model.model_fields.get(key) if model else None
        else:
            field = None if rest in SECTIONS or rest == "workspace" else Config.model_fields.get(rest)
        if field is None:
            log.warning("ignoring unknown environment variable %s", name)
            continue
        value = _env_value(environ[name], field.annotation)
        yield ({section: {key: value}} if nested else {rest: value}), f"env {name}"


def _env_value(raw: str, annotation: Any) -> Any:
    """Strings and paths stay as given (a numeric-looking token stays a string); other types
    are parsed as YAML so `false`, `30` and `[a, b]` work, then validated like file values.
    An optional field (`int | None`) is parsed like its inner type, so `null` turns it off."""
    if get_origin(annotation) in (Union, types.UnionType):
        inner = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(inner) == 1:
            annotation = inner[0]
    if annotation not in (bool, int, float) and get_origin(annotation) not in (list, dict):
        return raw
    return _yaml_value(raw)


def _yaml_value(raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _refuse_workspace_files(workspace: Any, files: list[tuple[Path, str]]) -> None:
    if not isinstance(workspace, (str, Path)):
        return  # validation reports the wrong type
    root = Path(workspace).expanduser().resolve()
    for path, label in files:
        if path.resolve().is_relative_to(root):
            raise ValueError(f"refusing to read {label}: it is inside the workspace, where the agent can change it")


def _leaf_keys() -> Iterator[str]:
    for name in Config.model_fields:
        if name in SECTIONS:
            yield from (f"{name}.{sub}" for sub in SECTIONS[name].model_fields)
        else:
            yield name


def describe_errors(exc: ValidationError, sources: Mapping[str, str], prefix: tuple[str, ...] = ()) -> str:
    """One line per problem: `<dotted.key>: <reason> (got <value>) [from <source>]`.

    `sources` is Config.sources; `prefix` is prepended to each error location, e.g.
    ("groups", "browser") for a model validated from that part of the config.
    """
    lines = []
    for error in exc.errors():
        loc = [*prefix, *(str(part) for part in error["loc"])]
        key = ".".join(loc)
        # A list item's location ends in its index; the source is recorded for the whole list.
        source = next((sources[k] for k in (".".join(loc[:n]) for n in range(len(loc), 0, -1)) if k in sources), None)
        where = f" [from {source}]" if source else ""
        if error["type"] == "extra_forbidden":
            lines.append(f"unknown config key: {key}{where}")
        elif error["type"] == "value_error":
            # Raised by a model validator about a combination of keys, so no single value to show.
            lines.append(f"{key or 'config'}: {error['msg'].removeprefix('Value error, ')}")
        else:
            reason = "Input should be a valid path" if error["type"] == "path_type" else error["msg"]
            lines.append(f"{key}: {reason} (got {error['input']!r}){where}")
    return "\n".join(lines)
