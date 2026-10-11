# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is 0.x, the plugin API (`dev_link_mcp.tools.base.ToolGroup`) and the config
format may change between minor releases; every such change is listed here.

## [Unreleased]

### Added

- `tunnel.ngrok_url` (`--ngrok-url`): with `--tunnel`, publish through ngrok on your free fixed
  domain instead of a Cloudflare quick tunnel. The token is kept per folder in the state dir, so
  the public URL stays the same across restarts and ChatGPT or Grok need no re-adding.
  `--new-token` replaces the kept token.
- The install script asks whether to install cloudflared, ngrok or both, and for ngrok adds the
  authtoken and writes the domain to the user config.

### Changed

- The commands that `dev-link start` suggests leave out the folder when you are already in it.

### Fixed

- `dev-link start`, `dev-link status` and the `serve` banner line up their labels when they show
  public URLs.

## [0.1.0]

First public release.

### Added

- An MCP server (streamable HTTP) that gives a coding agent one folder to work in: shell
  commands, files, search, background processes, project build/test/lint commands with parsed
  test results, code outlines, HTTP, data inspection, checkpoints and a task list. Browser tools
  (Playwright) and code tools (tree-sitter) are the `[browser]` and `[code]` extras; `[all]`
  installs both. Docker tools are opt-in with `--enable docker`.
- Commands run in a bubblewrap sandbox on Linux; the server refuses to start when bubblewrap
  cannot run, and `--no-sandbox` gives full user access instead. Optional memory and process
  limits run each command in a systemd user scope.
- The `dev-link` CLI: `serve`, `start`, `stop`, `restart`, `status`, `url`, `dashboard`, `logs`,
  `doctor`, `config`, `tools` and `install-browser`. Each folder gets its own server; `--tunnel`
  publishes it through a Cloudflare quick tunnel. The same CLI is also installed as `dev-link-mcp`,
  so `uvx dev-link-mcp` works.
- A live dashboard of tool calls, processes, tasks and workspace files, and an audit log of every
  call in SQLite and JSONL outside the workspace, pruned after `audit.retention_days` at startup
  and once a day.
- Layered config: defaults, `~/.config/dev-link/config.yaml`, `--config FILE`, `DEV_LINK_*`
  variables and flags, with per-group settings under `groups.<name>`.
- Tool group plugins through the `dev_link_mcp.tool_groups` entry point, loaded only when
  `tools.enabled` names them. The plugin API and config format are provisional.
- `mcp` is pinned below 3, because the tool schema trim relies on SDK internals.
- macOS is experimental: it runs only with `--no-sandbox` and is not tested in CI yet.

### Security

- With the network off, the HTTP tools refuse every request, the browser tools open only
  `about:blank`, pages load only `data:`, `blob:` and `about:` resources and WebRTC is off, and
  the process tools refuse to wait for a port. None of them can reach the host's loopback.
- With the network off, containers run with `--network none`, compose services share internal
  networks isolated from the host (Docker 28 or newer), and published ports, image pulls and
  image builds are refused. compose refuses volumes whose `driver_opts` type is anything but
  `none`, `bind` or `tmpfs`, so the daemon cannot mount network shares for the agent.
- The token is replaced with `***` in the audit log, background process logs, `server.log`,
  `tunnel.log` and server log lines. The startup banner and `dev-link start` show it only when
  their output is a terminal. A configured token is dropped with `auth: none`.
- The state dir, which holds the audit log, is made private (0700) even when it already exists.
  The server refuses to start, and `dev-link doctor` fails, when the sandbox would mount the state
  dir or when a `ro_paths` or `rw_paths` entry lies inside a folder the agent can write.
- If the server is killed or crashes, a small guard process kills cloudflared and `--no-sandbox`
  commands. A `--tunnel` server shuts down with exit status 1 when cloudflared exits, instead of
  running on with a dead public URL.

[Unreleased]: https://github.com/ajeetkharel/dev-link-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ajeetkharel/dev-link-mcp/releases/tag/v0.1.0
