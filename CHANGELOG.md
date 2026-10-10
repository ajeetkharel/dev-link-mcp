# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is 0.x, the plugin API (`dev_link_mcp.tools.base.ToolGroup`) and the config
format may change between minor releases; every such change is listed here.

## [Unreleased]

### Changed

- With the network off, `http_request`, `fetch_url`, `download_file` and the browser no longer
  reach the host's loopback. The HTTP tools refuse every request, and the browser loads only
  `about:`, `data:` and `blob:` URLs, with WebRTC turned off. `start_process` and
  `wait_for_process` refuse to wait for a port.
- With the network off, the docker group no longer gives containers network access. Containers
  run with `--network none`, compose services share internal networks isolated from the host
  (Docker 28 or newer), and published ports, image pulls and image builds are refused.
- compose refuses volumes whose `driver_opts` type is anything but `none`, `bind` or `tmpfs`, so
  the daemon cannot mount network shares for the agent.
- The startup banner shows the token only when its output is a terminal. uvicorn's log lines
  now use the server's log format.

### Fixed

- The server token no longer reaches background process logs, `server.log`, `tunnel.log` or
  server log lines on disk; it is replaced with `***` before it is written.
- The server refuses to start, and `dev-link doctor` fails, when the sandbox would mount the state
  dir, which holds the audit log and the token, or when a `ro_paths` or `rw_paths` entry lies
  inside a folder the agent can write.
- Old audit events and process logs are also pruned once a day while the server runs, not only
  at startup. Logs of processes the server still lists are kept.
- A configured token is dropped with `auth: none`, instead of being masked in logs.

- SIGKILL or a crash of the server no longer leaves cloudflared or `--no-sandbox` commands
  running. A small guard process kills them when the server is gone.
- A `--tunnel` server now notices when cloudflared exits. It shuts down with exit status 1 and
  shows the end of `tunnel.log`, instead of running on with a dead public URL.
- `mcp` is pinned below 3: the tool schema trim relies on SDK internals.

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
  call in SQLite and JSONL outside the workspace, pruned after `audit.retention_days`.
- Layered config: defaults, `~/.config/dev-link/config.yaml`, `--config FILE`, `DEV_LINK_*`
  variables and flags, with per-group settings under `groups.<name>`.
- Tool group plugins through the `dev_link_mcp.tool_groups` entry point, loaded only when
  `tools.enabled` names them. The plugin API and config format are provisional.

[Unreleased]: https://github.com/ajeetkharel/dev-link-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ajeetkharel/dev-link-mcp/releases/tag/v0.1.0
