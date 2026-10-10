"""Tool groups: the built-in table, third-party plugins, and registering them on a server.

A group is a ToolGroup subclass (tools/base.py). Plugins are found through the
`dev_link_mcp.tool_groups` entry point and loaded only when `tools.enabled` names them, so
installing a package never adds tools by itself.
"""

from __future__ import annotations

import asyncio
import difflib
import importlib
import importlib.metadata
import importlib.util
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ValidationError

from dev_link_mcp.config import describe_errors
from dev_link_mcp.runtime import Runtime
from dev_link_mcp.tools.base import ToolGroup

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "dev_link_mcp.tool_groups"


class Builtin(NamedTuple):
    """A built-in group: its name, `module:Class` target and the modules it needs.

    A group with requirements has a pyproject extra of the same name that installs them, and lists
    its tool names so `tools.disabled_tools` can name them on machines where the group is unavailable.
    """

    name: str
    target: str
    requires: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


BUILTINS: tuple[Builtin, ...] = (
    Builtin("system", "dev_link_mcp.tools.system:SystemGroup"),
    Builtin("files", "dev_link_mcp.tools.files:FilesGroup"),
    Builtin("search", "dev_link_mcp.tools.search:SearchGroup"),
    Builtin("shell", "dev_link_mcp.tools.shell:ShellGroup"),
    Builtin("processes", "dev_link_mcp.tools.processes:ProcessesGroup"),
    Builtin(
        "code",
        "dev_link_mcp.tools.code:CodeGroup",
        ("tree_sitter_language_pack",),
        ("code_outline", "code_stats", "find_references", "find_symbol"),
    ),
    Builtin("project", "dev_link_mcp.tools.project:ProjectGroup"),
    Builtin("http", "dev_link_mcp.tools.http:HttpGroup"),
    Builtin("data", "dev_link_mcp.tools.data:DataGroup"),
    Builtin("checkpoints", "dev_link_mcp.tools.checkpoints:CheckpointsGroup"),
    Builtin("tasks", "dev_link_mcp.tools.tasks:TasksGroup"),
    Builtin("docker", "dev_link_mcp.tools.docker:DockerGroup"),
    Builtin(
        "browser",
        "dev_link_mcp.tools.browser:BrowserGroup",
        ("playwright",),
        (
            "browser_click", "browser_close", "browser_console", "browser_eval", "browser_list_pages",
            "browser_navigate", "browser_network", "browser_open", "browser_press", "browser_screenshot",
            "browser_select", "browser_set_viewport", "browser_snapshot", "browser_type", "browser_wait",
        ),
    ),
)  # fmt: skip


class ToolGroupError(ValueError):
    """Tool group configuration that stops the server from starting; one line per problem."""


@dataclass
class Toolset:
    """The tool groups of one server, as set up by register_groups.

    `groups`: registered group instances by name, in registration order.
    `tools`: registered group name -> its tool names, sorted, without `tools.disabled_tools`.
    `unavailable`: enabled built-in group whose packages are missing -> install hint.
    `disabled`: groups turned off by `tools.disabled`, and opt-in built-ins that are not enabled.
    """

    groups: dict[str, ToolGroup] = field(default_factory=dict)
    tools: dict[str, list[str]] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    disabled: list[str] = field(default_factory=list)

    async def shutdown(self) -> None:
        """Await shutdown() of every registered group, all at once. A failure is logged and the
        others still run."""

        async def one(name: str, group: ToolGroup) -> None:
            try:
                await group.shutdown()
            except Exception:
                log.exception("shutdown of tool group %s failed", name)

        # Concurrently, so slow groups (docker stopping containers, the browser) share the few
        # seconds `dev-link stop` allows before SIGKILL instead of adding up.
        await asyncio.gather(*(one(name, group) for name, group in self.groups.items()))


def missing_requirements(builtin: Builtin) -> list[str]:
    """The modules a built-in group needs that are not installed; found without importing them."""
    return [module for module in builtin.requires if not _installed(module)]


def install_hint(builtin: Builtin) -> str:
    """The command that installs a built-in group's requirements: its pyproject extra."""
    return f"pip install 'dev-link-mcp[{builtin.name}]'"


def load_group_class(target: str) -> type[ToolGroup]:
    """Import `module:Class` and return the class. Raises ImportError or AttributeError."""
    module_name, _, class_name = target.partition(":")
    cls: type[ToolGroup] = getattr(importlib.import_module(module_name), class_name)
    return cls


async def register_groups(mcp: MCPServer, rt: Runtime) -> Toolset:
    """Register every enabled, installed tool group on `mcp`, remove `tools.disabled_tools` and
    trim the published schemas (trim_tool_schemas).

    A group is enabled when it is not opt-in or `tools.enabled` lists it, and `tools.disabled`
    does not; plugins always need `tools.enabled`. An enabled built-in whose required modules
    are missing is unavailable: it is never imported, and its install hint is logged at INFO and
    stored in `rt.unavailable_groups`. Each group's `groups.<name>` section is validated with
    its Settings model.

    Raises ToolGroupError for an unknown group name in `tools.enabled`, `tools.disabled` or
    `groups`; an enabled plugin that fails to import, is not a ToolGroup subclass or is installed
    by more than one package; invalid group settings; and a `tools.disabled_tools` name that
    matches no tool. A plugin with a built-in's name is ignored with a warning.
    Groups created before the error are shut down.
    """
    config = rt.config
    sources = config.sources
    builtins = {b.name: b for b in BUILTINS}
    plugins, ambiguous = _plugins()
    for name in sorted(builtins.keys() & plugins.keys()):
        # `tools.enabled` always refers to the built-in group, so a plugin with the same name is unreachable.
        log.warning("ignoring plugin tool group %r (%s): a built-in group has that name", name, plugins.pop(name).value)
    known = sorted(builtins.keys() | plugins.keys())
    errors: list[str] = []
    for key in ("enabled", "disabled"):
        for name in getattr(config.tools, key):
            if name not in known:
                errors.append(
                    f"tools.{key}: unknown tool group; known groups: {', '.join(known)}"
                    f" (got {name!r}){_source(sources, f'tools.{key}')}"
                )
    for name in config.groups:
        if name not in known:
            errors.append(
                f"groups.{name}: unknown tool group; known groups: {', '.join(known)}"
                f"{_source(sources, f'groups.{name}')}"
            )
    _raise(errors)

    toolset = Toolset()
    classes: dict[str, type[ToolGroup]] = {}
    for b in BUILTINS:
        if b.name in config.tools.disabled:
            toolset.disabled.append(b.name)
            continue
        missing = missing_requirements(b)
        if missing:
            hint = install_hint(b)
            log.info(
                "tool group %s is unavailable (%s not installed); install it with: %s", b.name, ", ".join(missing), hint
            )
            toolset.unavailable[b.name] = hint
            continue
        cls = load_group_class(b.target)
        if cls.opt_in and b.name not in config.tools.enabled:
            toolset.disabled.append(b.name)
            continue
        classes[b.name] = cls
    for name in dict.fromkeys(config.tools.enabled):
        if name not in plugins or name in builtins:
            continue
        if name in config.tools.disabled:
            toolset.disabled.append(name)
            continue
        if name in ambiguous:
            errors.append(f"plugin tool group {name!r} is installed by more than one package: {ambiguous[name]}")
            continue
        entry = plugins[name]
        try:
            loaded = entry.load()
        except Exception as exc:
            errors.append(f"plugin tool group {name!r} ({entry.value}) failed to import: {type(exc).__name__}: {exc}")
            continue
        if not (isinstance(loaded, type) and issubclass(loaded, ToolGroup)):
            errors.append(f"plugin tool group {name!r} ({entry.value}) is not a ToolGroup subclass")
            continue
        classes[name] = loaded
    _raise(errors)
    rt.unavailable_groups.update(toolset.unavailable)

    settings: dict[str, BaseModel] = {}
    for name, cls in classes.items():
        try:
            settings[name] = cls.Settings.model_validate(config.groups.get(name, {}))
        except ValidationError as exc:
            errors.append(describe_errors(exc, sources, ("groups", name)))
    _raise(errors)

    try:
        for name, cls in classes.items():
            group = cls(rt, settings[name])
            toolset.groups[name] = group
            before = await _tool_names(mcp)
            group.register(mcp)
            toolset.tools[name] = sorted(await _tool_names(mcp) - before)
        await _remove_disabled_tools(mcp, rt, toolset)
        try:
            trim_tool_schemas(mcp)
        except (AttributeError, TypeError, KeyError) as exc:
            # The trim only saves tokens and reads SDK internals; an mcp release that changes them
            # must not stop the server.
            log.warning(
                "could not trim tool schemas (mcp %s), publishing some of them untrimmed: %s: %s",
                _sdk_version(),
                type(exc).__name__,
                exc,
            )
    except BaseException:
        await toolset.shutdown()
        raise
    return toolset


# The output schema the SDK generates for a tool that returns a plain str, once titles are gone.
_WRAPPED_STR_SCHEMA = {"properties": {"result": {"type": "string"}}, "required": ["result"], "type": "object"}
# Keywords whose value is a mapping of names (not schema keywords) to subschemas.
_NAMED_SUBSCHEMAS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
# Keywords whose value is instance data, where a "title" key belongs to the data.
_DATA_KEYWORDS = frozenset({"default", "const", "enum", "examples"})


def trim_tool_schemas(mcp: MCPServer) -> None:
    """Shrink every registered tool's published schemas; tool names, arguments and results are unchanged.

    Drops the "title" strings pydantic puts on schema nodes ("Paths", "write_fileOutput"), which
    repeat the property or tool name. A tool that returns a plain str stops publishing an
    output schema, so its calls return the text once as content instead of also repeating it
    as `structuredContent: {"result": ...}`. Tools with real structured output keep both.
    Descriptions are dedented: before Python 3.13 a docstring keeps its source indentation,
    which costs clients tokens and makes the published text depend on the Python version.

    Relies on mcp.server.mcpserver internals: `MCPServer._tool_manager.list_tools()` returns the
    live Tool objects; `Tool.parameters` is only published, arguments are validated by
    `fn_metadata.arg_model`; `Tool.output_schema` is a cached_property over
    `fn_metadata.output_schema`; and `FuncMetadata.convert_result` returns content only when
    `output_schema` is None. tests/test_schemas.py fails if any of these stop holding.

    Raises AttributeError, TypeError or KeyError when the SDK no longer matches. Each tool is
    read fully before it is changed, and a tool whose published output schema does not follow
    the change gets its output metadata back, so a failure never leaves a tool publishing an
    output schema its results do not follow.
    """
    for tool in mcp._tool_manager.list_tools():
        metadata = tool.fn_metadata
        before = (metadata.output_schema, metadata.output_model, metadata.wrap_output)
        after = before
        parameters = _strip_titles(tool.parameters)
        if before[0] is not None:
            schema = _strip_titles(before[0])
            # (None, None, False) is the state the SDK builds for `structured_output=False`.
            after = (None, None, False) if before[2] and schema == _WRAPPED_STR_SCHEMA else (schema, *before[1:])
        tool.parameters = parameters
        tool.description = inspect.cleandoc(tool.description)
        metadata.output_schema, metadata.output_model, metadata.wrap_output = after
        # Drop the cached value: register_groups has already listed the tools before this runs.
        tool.__dict__.pop("output_schema", None)
        if tool.output_schema is not metadata.output_schema:
            # Publishing a schema the results no longer follow would break calls; undo this tool.
            metadata.output_schema, metadata.output_model, metadata.wrap_output = before
            tool.__dict__.pop("output_schema", None)
            raise TypeError(f"Tool.output_schema of {tool.name!r} no longer follows fn_metadata.output_schema")


def _sdk_version() -> str:
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return "version unknown"


def _strip_titles(node: Any) -> Any:
    """A copy of a JSON schema without "title" keywords; property and definition names are kept."""
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    if not isinstance(node, dict):
        return node
    stripped: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in _DATA_KEYWORDS:
            stripped[key] = value
        elif key in _NAMED_SUBSCHEMAS and isinstance(value, dict):
            stripped[key] = {name: _strip_titles(sub) for name, sub in value.items()}
        else:
            stripped[key] = _strip_titles(value)
    return stripped


async def _remove_disabled_tools(mcp: MCPServer, rt: Runtime, toolset: Toolset) -> None:
    wanted = list(dict.fromkeys(rt.config.tools.disabled_tools))
    owner = {tool: group for group, names in toolset.tools.items() for tool in names}
    unmatched = [name for name in wanted if name not in owner]
    if unmatched:
        inactive = await _inactive_tool_names(rt, toolset)
        known = sorted(owner.keys() | inactive)
        source = _source(rt.config.sources, "tools.disabled_tools")
        errors = []
        for name in unmatched:
            if name in inactive:
                continue
            close = difflib.get_close_matches(name, known, n=5)
            reason = f"no such tool; closest names: {', '.join(close)}" if close else "no such tool"
            errors.append(f"tools.disabled_tools: {reason} (got {name!r}){source}")
        _raise(errors)
    for name in wanted:
        if name in owner:
            mcp.remove_tool(name)
            toolset.tools[owner[name]].remove(name)


async def _inactive_tool_names(rt: Runtime, toolset: Toolset) -> set[str]:
    """Tool names of built-in groups that are unavailable or turned off.

    Only called when a disabled tool name matched nothing, so a normal start never imports or
    registers a turned-off group.
    """
    names: set[str] = set()
    for b in BUILTINS:
        if b.name not in toolset.unavailable and b.name not in toolset.disabled:
            continue
        if b.tools:
            names.update(b.tools)
            continue
        cls = load_group_class(b.target)
        scratch = MCPServer("dev-link-scratch")
        cls(rt, cls.Settings()).register(scratch)
        names |= await _tool_names(scratch)
    return names


def _plugins() -> tuple[dict[str, importlib.metadata.EntryPoint], dict[str, str]]:
    """Installed plugin entry points by group name, and names claimed by several packages.

    The second mapping is name -> the conflicting targets joined with " and ". It only matters
    when `tools.enabled` names that plugin, so an unused conflict never blocks the start.
    """
    found: dict[str, importlib.metadata.EntryPoint] = {}
    ambiguous: dict[str, list[str]] = {}
    for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        other = found.setdefault(entry.name, entry)
        if other.value != entry.value:
            ambiguous.setdefault(entry.name, [other.value]).append(entry.value)
    return found, {name: " and ".join(values) for name, values in ambiguous.items()}


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _source(sources: Mapping[str, str], key: str) -> str:
    """` [from <layer>]` for a dotted key, or for the first key below it (a `groups.<name>` entry)."""
    label = sources.get(key) or next((v for k, v in sources.items() if k.startswith(key + ".")), None)
    return f" [from {label}]" if label else ""


def _raise(errors: list[str]) -> None:
    if errors:
        raise ToolGroupError("\n".join(errors))


async def _tool_names(mcp: MCPServer) -> set[str]:
    return {t.name for t in await mcp.list_tools()}
