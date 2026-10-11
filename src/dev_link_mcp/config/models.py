"""Configuration models: every setting, its type, default and user-facing description.

The field descriptions are the source of docs/configuration.md and docs/config.schema.json.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, Strict, field_validator, model_validator

AuthMode = Literal["path-token", "bearer", "none"]
AUTH_MODES: tuple[AuthMode, ...] = ("path-token", "bearer", "none")

SandboxBackend = Literal["auto", "bubblewrap", "none"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_TOKEN_CHARS = re.compile(r"[A-Za-z0-9._~-]+")

# YAML and the command line give paths as strings; strict mode would only take Path objects.
LaxPath = Annotated[Path, Strict(False)]


def default_state_root() -> Path:
    """Parent of the default per-workspace state dirs: `$XDG_STATE_HOME/dev-link-mcp` (default `~/.local/state`)."""
    return Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "dev-link-mcp"


class Section(BaseModel):
    """Base for every config model.

    Strict so YAML's loose typing fails loudly (`network: "false"` is a truthy string,
    `allowed_hosts: x.com` is not a list), and validated on assignment so code and tests
    that change settings at runtime cannot store a wrong type either.
    """

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class ServerConfig(Section):
    host: str = Field(
        default="127.0.0.1", description="Address to bind. Auth `none` is only allowed on a loopback address."
    )
    port: int = Field(default=8765, ge=1, le=65535, description="TCP port to listen on.")
    auth: AuthMode = Field(
        default="path-token",
        description="How MCP clients prove access. `path-token` puts a secret token in the URL path, "
        "`bearer` expects an `Authorization: Bearer` header, `none` turns auth off (loopback only).",
    )
    token: str = Field(
        default="",
        description="Fixed token for `path-token` and `bearer` auth. Empty means a new random token on every start. "
        "At least 16 characters, using only letters, digits, `-`, `_`, `.` and `~`.",
    )
    public_url: str = Field(
        default="",
        description="Public base URL when the server sits behind a tunnel or proxy "
        "(e.g. `https://x.trycloudflare.com`). Its hostname is accepted automatically and the banner "
        "prints public URLs. Not allowed with auth `none`.",
    )
    allowed_hosts: list[str] = Field(
        default_factory=list,
        description="Extra Host header values to accept, e.g. a tunnel hostname. Exact match, or `host:*` for "
        "any port. Not allowed with auth `none`.",
    )

    @model_validator(mode="after")
    def _check_auth_none(self) -> ServerConfig:
        if self.auth != "none":
            return self
        if self.host not in LOOPBACK_HOSTS:
            raise ValueError("auth 'none' is only allowed on a loopback address")
        if self.public_url or self.allowed_hosts:
            # A tunnel or proxy makes a loopback bind public; with no token anyone who finds the URL can run commands.
            raise ValueError(
                "auth 'none' cannot be combined with --public-url or --allowed-host; use the default path-token mode,"
                " which puts a fresh secret token in the URL path on every start"
            )
        return self


class TunnelConfig(Section):
    ngrok_url: str = Field(
        default="",
        description="Your ngrok domain, e.g. `https://name.ngrok-free.app`. With `--tunnel` the server is "
        "published there through ngrok instead of a Cloudflare quick tunnel, and the token is kept in the state "
        "directory, so the public URL stays the same across restarts. Empty means a quick tunnel.",
    )

    @field_validator("ngrok_url")
    @classmethod
    def _check_ngrok_url(cls, value: str) -> str:
        value = value.strip().removesuffix("/")
        if not value:
            return value
        parts = urlsplit(value)
        # urlsplit drops an empty `?` or `#`, so look for them in the text itself.
        if parts.scheme != "https" or not parts.hostname or parts.path or "?" in value or "#" in value:
            raise ValueError("ngrok_url must be https://<host> with no path, e.g. https://name.ngrok-free.app")
        if parts.username or parts.password or parts.port:
            raise ValueError("ngrok_url must not contain a user, password or port")
        # The host in lower case, as ngrok reports it and as the server compares it with Host headers.
        return f"https://{parts.hostname}"


class SandboxConfig(Section):
    backend: SandboxBackend = Field(
        default="auto",
        description="How agent commands are isolated. `bubblewrap` confines them to the workspace (Linux only); "
        "`none` runs them with full access to your machine, like a coding agent run directly. `auto` uses "
        "bubblewrap and refuses to start when it cannot run here (not Linux, `bwrap` missing, or user namespaces "
        "blocked); it never falls back to `none`.",
    )
    network: bool = Field(default=True, description="Allow network access inside the sandbox.")
    ro_paths: list[str] = Field(
        default_factory=lambda: [
            "~/.nvm",
            "~/.local/bin",
            "~/.local/share/uv/python",
            "~/.sdkman",
            "~/.cargo/bin",
            "~/.rustup",
            "~/go",
            "~/.pyenv",
            "~/.bun",
            "~/.deno",
        ],
        description="Host paths mounted read-only inside the sandbox in addition to the system folders. "
        "Missing paths are skipped, so one config works across machines. None may lie inside the workspace "
        "or a `rw_paths` entry, where the agent could swap it for a symlink.",
    )
    rw_paths: list[str] = Field(
        default_factory=list,
        description="Host paths mounted read-write. Anything here is reachable by the agent. None may lie inside "
        "the workspace or another entry.",
    )
    env_passthrough: list[str] = Field(
        default_factory=lambda: ["LANG", "LC_ALL", "TZ", "JAVA_HOME", "MAVEN_HOME", "GOPATH", "NVM_DIR"],
        description="Host environment variables copied into the sandbox. Everything else is dropped so server "
        "secrets do not leak into agent commands.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for agent commands. Values must be strings: quote numbers in YAML.",
    )

    @property
    def enabled(self) -> bool:
        """True unless the backend is `none`; `auto` either confines commands or stops the server."""
        return self.backend != "none"


class LimitsConfig(Section):
    default_timeout_s: float = Field(
        default=120.0, description="Command timeout in seconds when the agent does not set one."
    )
    max_timeout_s: float = Field(default=3600.0, description="Longest timeout in seconds the agent may ask for.")
    max_output_bytes: int = Field(
        default=200_000, description="Bytes of command output returned to the agent; the middle is cut out beyond this."
    )
    max_read_bytes: int = Field(
        default=2_000_000, description="Largest file or HTTP body, in bytes, that read tools return."
    )
    max_processes: int = Field(default=32, description="Most background processes that may run at the same time.")
    memory_mb: int | None = Field(
        default=None,
        ge=1,
        description="Memory cap in MiB for each command and background process. Swap is not allowed, so past "
        "the cap the kernel kills the command. Enforced with a systemd user scope (`systemd-run --user`); the "
        "server refuses to start if that does not work. `null` means no cap.",
    )
    max_pids: int | None = Field(
        default=None,
        ge=1,
        description="Most processes and threads each command may have at once, counting the two that bubblewrap "
        "itself uses; further forks fail. JVM and Node runtimes start many threads, so leave room. Enforced like "
        "`memory_mb`. `null` means no cap.",
    )


class AuditConfig(Section):
    result_bytes: int = Field(
        default=256_000,
        description=(
            "Bytes of each tool call's arguments and result kept in the audit log. Longer arguments are"
            " stored as a clipped string instead of a JSON object."
        ),
    )
    retention_days: int | None = Field(
        default=30,
        ge=1,
        description="Audit events and background process logs older than this many days are deleted when the "
        "server starts and once a day while it runs. Logs of processes the server still lists are kept. "
        "`null` keeps everything.",
    )


class ToolsConfig(Section):
    enabled: list[str] = Field(
        default_factory=list,
        description="Groups to turn on that are off by default: the opt-in group `docker` and installed plugins.",
    )
    disabled: list[str] = Field(default_factory=list, description="Tool groups to turn off, e.g. `http` or `browser`.")
    disabled_tools: list[str] = Field(
        default_factory=list,
        description="Single tools to remove, e.g. `run_command`. Names of tools in a group that is turned off "
        "or not installed are ignored, so one list works across machines.",
    )


class Config(Section):
    workspace: LaxPath = Field(
        description="Folder exposed to the agent and the only place it can write. Usually given as the "
        "command-line argument. Must not be `/`, your home folder, or a parent of it."
    )
    state_dir: LaxPath | None = Field(
        default=None,
        description="Where logs, the audit store and the sandbox home live. Must be outside the workspace and, "
        "with a sandbox, outside every folder it mounts (system folders, `sandbox.ro_paths`, `sandbox.rw_paths`) "
        "with none of them inside it. Default: `$XDG_STATE_HOME/dev-link-mcp/<folder name>-<hash>`.",
    )
    log_level: LogLevel = Field(default="INFO", description="Server log level. Lower case is accepted.")
    server: ServerConfig = Field(default_factory=ServerConfig, description="Network address and authentication.")
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig, description="How `--tunnel` publishes the server.")
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig, description="Isolation of agent commands.")
    limits: LimitsConfig = Field(
        default_factory=LimitsConfig, description="Timeouts, size limits and resource caps for tools."
    )
    audit: AuditConfig = Field(default_factory=AuditConfig, description="What the audit log keeps.")
    tools: ToolsConfig = Field(default_factory=ToolsConfig, description="Which tool groups are registered.")
    groups: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Settings of each tool group, keyed by group name. Each group checks its own keys at start; "
        "see Tool groups below.",
    )

    _sources: dict[str, str] = PrivateAttr(default_factory=dict)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @property
    def sources(self) -> dict[str, str]:
        """Dotted key -> where its value came from ("default", "cli", "env DEV_LINK_...", a file).

        Filled by load_config; empty for a Config built directly.
        """
        return self._sources

    def finalize(self) -> Config:
        """Resolve paths, generate the token (or clear it with auth `none`) and validate. Returns
        self for chaining.

        Raises ValueError for a missing workspace, a workspace that is home or contains it, a bad
        token, or a state directory inside the workspace (the agent could then rewrite its own
        audit log). Creates the state directory (mode 0700).
        """
        self.workspace = Path(self.workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace does not exist or is not a directory: {self.workspace}")
        # An ancestor of home (including /) would also mount the real home over the sandbox's private one.
        if Path.home().resolve().is_relative_to(self.workspace):
            raise ValueError("refusing to expose /, your home directory, or a parent of it as the workspace")
        server = self.server
        if server.auth == "none":
            # Every log redacts the token, so a leftover one would turn its text into *** everywhere.
            server.token = ""
        elif not server.token:
            server.token = secrets.token_urlsafe(24)
        if server.auth != "none" and len(server.token) < 16:
            raise ValueError("token must be at least 16 characters")
        # The token goes into URL paths and an HTTP header, so only URL-safe characters work everywhere.
        if server.auth != "none" and not _TOKEN_CHARS.fullmatch(server.token):
            raise ValueError("token may only contain letters, digits, '-', '_', '.' and '~'")
        if self.state_dir is None:
            digest = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:10]
            self.state_dir = default_state_root() / f"{self.workspace.name}-{digest}"
        self.state_dir = Path(self.state_dir).expanduser().resolve()
        if self.state_dir.is_relative_to(self.workspace):
            raise ValueError("state_dir must be outside the workspace so the agent cannot edit its own logs")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode only applies to a new directory; one that already exists, such as a
        # --state-dir picked by hand, may be open to other users and holds the audit log.
        if self.state_dir.stat().st_mode & 0o077:
            try:
                self.state_dir.chmod(0o700)
            except OSError as e:
                message = f"state_dir {self.state_dir} is open to other users and cannot be made private: {e}"
                raise ValueError(message) from e
        return self

    @property
    def mcp_path(self) -> str:
        return f"/mcp/{self.server.token}" if self.server.auth == "path-token" else "/mcp"

    @property
    def ui_path(self) -> str:
        # Browsers cannot attach a bearer header to a page load, so the dashboard
        # always uses a secret path unless auth is off entirely.
        return "/ui" if self.server.auth == "none" else f"/ui/{self.server.token}"

    def base_url(self) -> str:
        # A wildcard bind is reachable on loopback, which is the URL to print.
        host = "127.0.0.1" if self.server.host in ("0.0.0.0", "::") else self.server.host  # noqa: S104
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.server.port}"
