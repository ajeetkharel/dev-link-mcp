<p align="center">
  <img src="https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/docs/assets/logo.png" width="112" alt="dev-link-mcp logo">
</p>

<h1 align="center">dev-link-mcp</h1>

<!-- mcp-name: io.github.ajeetkharel/dev-link-mcp -->

<p align="center">
  Turn the AI you already use, whether ChatGPT, Grok, Claude Code, Codex, Cursor or VS Code, into a coding agent for one folder on your machine, sandboxed and logged on a live dashboard.
</p>

<p align="center">
  <a href="https://pypi.org/project/dev-link-mcp/"><img src="https://img.shields.io/pypi/v/dev-link-mcp" alt="PyPI version"></a>
  <a href="https://pypi.org/project/dev-link-mcp/"><img src="https://img.shields.io/pypi/pyversions/dev-link-mcp" alt="Python versions"></a>
  <a href="https://github.com/ajeetkharel/dev-link-mcp/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/dev-link-mcp" alt="License"></a>
  <a href="https://github.com/ajeetkharel/dev-link-mcp/actions/workflows/ci.yml"><img src="https://github.com/ajeetkharel/dev-link-mcp/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
</p>

![The dev-link dashboard during an agent session](https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/docs/assets/dashboard.png)

**What.** dev-link is an MCP server that runs on your machine and exposes one project folder:
files, search, a shell, background processes, a headless browser and checkpoints.

**Why.** Web chats such as ChatGPT and Grok can add a custom MCP server, and so can every coding
agent and editor, including Claude Code, Codex CLI, Cursor and VS Code. dev-link gives any of them
the same development environment, with no API key and no extra model bill. The folder is sandboxed,
and every call is logged on a dashboard you can watch.

**How.** Start a server for the folder, then paste its URL into your client as a remote MCP server.
Any client that gains MCP support later works the same way.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/scripts/install.sh | sh
cd your-project && dev-link start . --tunnel    # prints the URL to give your client
```

Copy the `public MCP URL` from the output and add it to your AI client as a custom (remote) MCP
server with no authentication. [What you will see](#what-you-will-see) shows the output, and
[Add it to your client](#add-it-to-your-client) says where to paste it.

Using a client on this machine (Claude Code, Codex, Cursor, VS Code)? Drop `--tunnel`.

The [install script](https://github.com/ajeetkharel/dev-link-mcp/blob/main/scripts/install.sh) installs uv, dev-link, Chromium for the browser tools
and cloudflared, and on Linux the bubblewrap sandbox, git and ripgrep through your package manager,
so it asks for sudo. To install by hand instead, see [Requirements](#requirements).

## Why

The model stays in your client. dev-link talks to no model and needs no API key, so the plan you
already pay for does the work. dev-link uses only standard MCP over Streamable HTTP, with the token
in the URL or in a bearer header, so it works with any client that can add a remote MCP server
that way.

dev-link adds what a chat window or an editor lacks: your files, a shell and a browser.

You expose one folder, not your machine. On Linux every command runs in a bubblewrap sandbox
where that folder is writable, system tools are read-only, and your home, SSH keys and other
projects are not there.

Every tool call is saved with its arguments and result to an audit log outside the folder. The
dashboard shows the calls as they happen, along with running processes, the agent's task list
and the workspace files.

The agent gets a development environment, not just file access. It can run any command, install
packages, start a dev server and wait for its port, click through the app in a headless browser,
and take checkpoints it can diff and restore.

## What you will see

`dev-link start . --tunnel` prints this (the token and hostname are random on every start):

```text
dev-link started for /home/you/your-project

  MCP URL   : http://127.0.0.1:8765/mcp/<token>
  dashboard : http://127.0.0.1:8765/ui/<token>/
  public MCP URL: https://<random>.trycloudflare.com/mcp/<token>
  public dash: https://<random>.trycloudflare.com/ui/<token>/
  PID       : 371145
  logs      : /home/you/.local/state/dev-link-mcp/your-project-<id>/server.log

The token in these URLs is new on every start; anyone with the URL can use the server.
The public URL is reachable from the internet: anyone with it can run commands here.
```

- **Give your client the `public MCP URL`.** `dev-link url .` prints the same line on its own,
  which is handy for `$(...)` in a command.
- **Without `--tunnel`** there are no public lines. Clients on your machine use the `MCP URL`.
- **Watch the agent work** by opening the `dashboard` URL from your own machine. Keep the
  `public dash` line to yourself.
- **Check or stop the server** with `dev-link status .` and `dev-link stop .`. A restart gives a
  new URL, so add the server to your client again.

## Add it to your client

| Client | Run `start` | Where to paste the URL |
| --- | --- | --- |
| ChatGPT | with `--tunnel` | [chatgpt.com/plugins](https://chatgpt.com/plugins), plus button, **Add custom MCP server**, **No authentication** |
| Grok | with `--tunnel` | [grok.com/connectors](https://grok.com/connectors), **New Connector**, **Custom** |
| Claude Code, Codex CLI, Cursor, VS Code | without `--tunnel` | the commands and files below |
| Any other MCP client | `--tunnel` for web, none for local | its "add remote MCP server" setting |

Web chats are covered step by step in [docs/clients/web.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md), and
[docs/clients](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/README.md) covers every client, fixed URLs, bearer auth and how to
check the connection.

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

## Requirements

The install script sets all of this up. By hand:

```bash
sudo apt install bubblewrap git ripgrep        # or dnf, pacman, zypper, brew
uv tool install --python 3.13 'dev-link-mcp[all] @ git+https://github.com/ajeetkharel/dev-link-mcp'
dev-link install-browser                       # Chromium for the browser tools
```

- **bubblewrap** (Linux): the sandbox. `dev-link start` refuses to run without it, unless you pass
  `--no-sandbox`, which gives the agent your full user access. Ubuntu 23.10 and later block the
  user namespaces bubblewrap needs; see the [AppArmor note](https://github.com/ajeetkharel/dev-link-mcp/blob/main/CONTRIBUTING.md#setup).
- **git and ripgrep**: used by the tools (`sudo apt install git ripgrep`).
- **cloudflared**: only for `--tunnel`. See [Install cloudflared](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md#install-cloudflared).
- **macOS**: runs only with `--no-sandbox`. With `--tunnel` as well, anyone with the public URL gets
  your full user access.

`dev-link doctor` checks each of these and says how to fix what is missing.

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
- [Clients](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/README.md): [web](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md) (ChatGPT, Grok) and [local](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/local.md) (Claude Code, Codex CLI, Cursor, VS Code).
- [Architecture](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/architecture.md): how a tool call flows and where state lives.
- [Security](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/security.md): what the sandbox holds back and what it does not.
- [Extending](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/extending.md): tool groups from other packages.

`dev-link --help` lists every command.

## Contributing

See [CONTRIBUTING.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/CONTRIBUTING.md).

## License

MIT License. See [LICENSE](https://github.com/ajeetkharel/dev-link-mcp/blob/main/LICENSE).
