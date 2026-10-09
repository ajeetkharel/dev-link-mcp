<p align="center">
  <img src="https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/docs/assets/logo.png" width="112" alt="dev-link-mcp logo">
</p>

<h1 align="center">dev-link-mcp</h1>

<!-- mcp-name: io.github.ajeetkharel/dev-link-mcp -->

<p align="center">
  An MCP server that gives a coding agent one folder to work in, sandboxed and logged on a live dashboard.
</p>

<p align="center">
  <a href="https://pypi.org/project/dev-link-mcp/"><img src="https://img.shields.io/pypi/v/dev-link-mcp" alt="PyPI version"></a>
  <a href="https://pypi.org/project/dev-link-mcp/"><img src="https://img.shields.io/pypi/pyversions/dev-link-mcp" alt="Python versions"></a>
  <a href="https://github.com/ajeetkharel/dev-link-mcp/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/dev-link-mcp" alt="License"></a>
  <a href="https://github.com/ajeetkharel/dev-link-mcp/actions/workflows/ci.yml"><img src="https://github.com/ajeetkharel/dev-link-mcp/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
</p>

![The dev-link dashboard during an agent session](https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/docs/assets/dashboard.png)

## Quick start

```bash
uv tool install 'dev-link-mcp[all]'     # or: pipx install 'dev-link-mcp[all]'
dev-link install-browser                # once, downloads Chromium for the browser tools
dev-link start .                        # background server for the current folder
claude mcp add --transport http dev-link "$(dev-link url)"
```

`dev-link dashboard` prints the dashboard URL, and `dev-link stop` stops the server. If something
fails, `dev-link doctor` says what is missing and how to fix it.

On Linux the sandbox needs bubblewrap (`apt install bubblewrap`), and the tools use git and
ripgrep. Ubuntu 23.10 and later block the user namespaces bubblewrap needs; see the
[AppArmor note](https://github.com/ajeetkharel/dev-link-mcp/blob/main/CONTRIBUTING.md#setup).

## Why

You expose one folder, not your machine. On Linux every command runs in a bubblewrap sandbox
where that folder is writable, system tools are read-only, and your home, SSH keys and other
projects are not there.

Every tool call is saved with its arguments and result to an audit log outside the folder. The
dashboard shows the calls as they happen, along with running processes, the agent's task list
and the workspace files.

The agent gets a development environment, not just file access. It can run any command, install
packages, start a dev server and wait for its port, click through the app in a headless browser,
and take checkpoints it can diff and restore.

## Connecting a client

dev-link speaks MCP over Streamable HTTP. Every client takes the URL from `dev-link url`, which
changes on every start. [docs/clients.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients.md)
covers fixed URLs, bearer auth and how to check the connection.

<details>
<summary>Claude Code</summary>

```bash
claude mcp add --transport http dev-link "$(dev-link url DIR)"
```

After a restart, run `claude mcp remove dev-link` and add it again.
</details>

<details>
<summary>Codex CLI</summary>

```bash
codex mcp add dev-link --url "$(dev-link url DIR)"
```

Codex waits 60 s for a tool call by default, less than dev-link's command timeout, so raise it in
`~/.codex/config.toml`:

```toml
[mcp_servers.dev-link]
url = "http://127.0.0.1:8765/mcp/<token>"
tool_timeout_sec = 600
```
</details>

<details>
<summary>Cursor</summary>

`~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` in the project root:

```json
{
  "mcpServers": {
    "dev-link": { "url": "http://127.0.0.1:8765/mcp/<token>" }
  }
}
```
</details>

<details>
<summary>VS Code</summary>

`.vscode/mcp.json` in the workspace, or your user `mcp.json`:

```json
{
  "servers": {
    "dev-link": { "type": "http", "url": "http://127.0.0.1:8765/mcp/<token>" }
  }
}
```
</details>

<details>
<summary>ChatGPT</summary>

ChatGPT only connects to servers it can reach from the internet over HTTPS, so publish the
server through a tunnel (needs `cloudflared`):

```bash
dev-link start DIR --tunnel
dev-link url DIR                # https://<random>.trycloudflare.com/mcp/<token>
```

Add it as a custom MCP server with no authentication; the token in the URL is the secret.
Anyone with that URL can run commands on your machine.
</details>

## Tools

Tools come in groups. All groups are on by default except docker.

| Group | For | Default | Extra |
| --- | --- | --- | --- |
| `system` | workspace and server info | on | |
| `files` | read, write, edit, move and delete files | on | |
| `search` | find files, grep, search and replace (ripgrep) | on | |
| `shell` | run commands and code snippets | on | |
| `processes` | dev servers and watchers in the background | on | |
| `code` | outlines, definitions and references (tree-sitter) | on | `code` |
| `project` | detect the stack; install, build, lint, format, test | on | |
| `http` | HTTP requests, fetch pages, downloads | on | |
| `data` | SQLite, jq and CSV | on | |
| `checkpoints` | snapshot, diff and restore the workspace | on | |
| `tasks` | a persistent task list and notes | on | |
| `browser` | a headless Chromium (Playwright) | on | `browser` |
| `docker` | containers and compose, limited to the workspace | opt-in (`--enable docker`) | |

A group whose extra is missing is reported as not installed. Git, package managers and any
other CLI run through `run_command`, as in your own terminal. Every tool and its parameters are
listed in [docs/tools.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/tools.md).

## Security

The MCP URL contains a random token that changes on every start. Anyone with the URL can run
commands in the folder, so treat it like a password. On Linux, commands run in a bubblewrap
sandbox that sees the folder, read-only system paths and toolchains, and a private home. The
network is on by default, so the agent can reach the internet, your LAN and services on
localhost; `--no-network` turns it off. `--no-sandbox` gives the agent your full user access,
and the docker group is root-equivalent when you enable it. Details are in
[docs/security.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/security.md);
report vulnerabilities as described in
[SECURITY.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/SECURITY.md).

## Platforms

| Platform | Support |
| --- | --- |
| Linux | Supported. Commands run in a bubblewrap sandbox. |
| macOS | Only with `--no-sandbox`, which gives the agent your full user access. |
| Windows | Not supported. |

## Documentation

- [Configuration](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/configuration.md): config files, environment variables and every setting.
- [Tools](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/tools.md): every tool with its parameters.
- [Clients](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients.md): Claude Code, Codex CLI, Cursor, VS Code and ChatGPT.
- [Architecture](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/architecture.md): how a tool call flows and where state lives.
- [Security](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/security.md): what the sandbox holds back and what it does not.
- [Extending](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/extending.md): tool groups from other packages.

`dev-link --help` lists every command.

## Contributing

See [CONTRIBUTING.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/CONTRIBUTING.md).

## License

MIT License. See [LICENSE](https://github.com/ajeetkharel/dev-link-mcp/blob/main/LICENSE).
