<p align="center">
  <img src="https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/docs/assets/logo.png" width="112" alt="dev-link-mcp logo">
</p>

<h1 align="center">dev-link-mcp</h1>

<!-- mcp-name: io.github.ajeetkharel/dev-link-mcp -->

<p align="center">
  <strong>Your AI. Your project. A real development workspace.</strong>
</p>

<p align="center">
  Connect your AI client to a project on your machine. Edit code, run tests, manage dev servers and drive a browser, with a live dashboard to follow the work.
</p>

<p align="center">
  <a href="https://pypi.org/project/dev-link-mcp/"><img src="https://img.shields.io/pypi/v/dev-link-mcp" alt="PyPI version"></a>
  <a href="https://pypi.org/project/dev-link-mcp/"><img src="https://img.shields.io/pypi/pyversions/dev-link-mcp" alt="Python versions"></a>
  <a href="https://github.com/ajeetkharel/dev-link-mcp/blob/main/LICENSE"><img src="https://img.shields.io/github/license/ajeetkharel/dev-link-mcp" alt="License"></a>
  <a href="https://github.com/ajeetkharel/dev-link-mcp/actions/workflows/ci.yml"><img src="https://github.com/ajeetkharel/dev-link-mcp/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
</p>

![The dev-link dashboard during an agent session: activity feed, background processes, workspace files, tasks and notes](https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/docs/assets/dashboard.gif)

[Quick start](#quick-start) · [Capabilities](#capabilities) · [Tunnels](#tunnels) · [Security](#security) · [Documentation](#documentation)

## What is dev-link?

dev-link is an open-source [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
server that gives an AI client tools to work on a project folder on your machine.
Your client sends tool calls; dev-link runs them and returns the results.

Use it to bring a local project into a web chat such as ChatGPT or Grok, or to give an editor or
CLI agent the same workspace tools and dashboard. It works with clients that support remote MCP
servers over Streamable HTTP, including Claude Code, Codex CLI, Cursor and VS Code.

dev-link runs no model and requires no model API key of its own. Your AI client's plan,
usage limits and MCP support still apply.

- **Work on the actual project.** Read and edit files, search code, run builds and tests, and
  keep development servers running between tool calls.
- **Check the app in a browser.** Navigate pages, interact with the UI, inspect console and
  network activity, and take screenshots with Playwright.
- **Follow the work.** Watch tool calls and results, running processes, files, tasks and notes
  in the dashboard. Tool calls are also recorded in an audit log.
- **Save a recovery point.** Create, compare and restore workspace checkpoints before risky edits.
- **Choose how to connect.** Connect locally, or use built-in ngrok and Cloudflare tunnel support
  for web clients. Linux commands run in a bubblewrap sandbox by default.

**Status:** Alpha. Linux is supported; macOS is experimental and requires `--no-sandbox`.
Windows is not supported. See [platforms](#platforms) and [security](#security) before starting.

## Quick start

### 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/scripts/install.sh | sh
```

The [install script](https://github.com/ajeetkharel/dev-link-mcp/blob/main/scripts/install.sh)
sets up uv, dev-link, Chromium and system dependencies. On Linux it uses your package manager
for bubblewrap, git and ripgrep and may request sudo. Open a new terminal if `dev-link` is
not found after installation.

In an interactive terminal, the installer offers ngrok, cloudflared, both or neither.
Choose **ngrok** for a reusable public URL or **cloudflared** for a temporary URL without an
account. ngrok setup needs your account's authtoken and assigned domain. Without a terminal,
the installer defaults to cloudflared unless configured otherwise.

Prefer to install dependencies yourself? See [manual installation](#manual-installation).

### 2. Start in your project folder

For a **web client**, start with a public tunnel:

```bash
cd ~/code/my-project
dev-link start --tunnel
```

For an **editor or CLI on the same machine**, start locally:

```bash
cd ~/code/my-project
dev-link start
```

The startup output includes connection and dashboard URLs. For example, with ngrok configured:

```text
MCP URL        : http://127.0.0.1:8765/mcp/<token>
dashboard      : http://127.0.0.1:8765/ui/<token>/
public MCP URL : https://<your-ngrok-domain>/mcp/<token>
public dash    : https://<your-ngrok-domain>/ui/<token>/
```

These are illustrative; use the URLs printed by your server. The port may differ.
Without `--tunnel`, only local URLs are printed.

**Treat these URLs like passwords.** They contain an access token. Anyone with the token can
use the server's tools, including running commands. Keep the dashboard URL private and read
[Security](#security) before exposing a workspace.

### 3. Connect your client

| Client | Connect with | Setup guide |
| --- | --- | --- |
| ChatGPT or Grok | Add the **public MCP URL** as a custom MCP server/connector | [Web clients](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md) |
| Claude Code | `claude mcp add --transport http dev-link "$(dev-link url)"` | [Local clients](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/local.md) |
| Codex CLI | `codex mcp add dev-link --url "$(dev-link url)"` | [Local clients](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/local.md) |
| Cursor or VS Code | Add the **MCP URL** to your MCP configuration | [Local clients](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/local.md) |
| Other MCP clients | Add the appropriate URL using **Streamable HTTP** | [Connection and authentication](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/README.md) |

Run CLI setup commands from the project folder. `dev-link url` prints the public URL when a
tunnel is active, otherwise the local URL. For the default path-token setup, choose
**No authentication** if your web client asks: the secret is already in the URL.
Client availability can depend on your plan and workspace settings.

Open the **local dashboard URL** in your browser to follow the session. `dev-link dashboard`
prints it again.

### 4. Try it

Start with a task that helps the client understand your project:

> Inspect this project, explain how to run it, run the existing tests and report any failures.
> Don't change any files yet.

Then try a development workflow:

> Start the development server, open the app in the browser and check the main user flow.
> Include a screenshot and any console errors in your report.

Or make a focused change:

> Create a checkpoint, fix the failing test, and run the relevant tests again.
> Show me the diff and explain what changed.

When you're done, run `dev-link stop` to stop the server and its tunnel.

## Capabilities

| What you need | What dev-link provides |
| --- | --- |
| Files and search | Read, write and patch files; search with ripgrep; replace across files |
| Commands and processes | Run shell commands and code snippets; manage background servers and watchers |
| Code navigation | Inspect outlines, definitions and references with tree-sitter |
| Build and test | Detect the project stack; install dependencies; build, lint, format and test |
| Browser automation | Drive Chromium with Playwright; inspect pages, screenshots, console and network activity |
| APIs and data | Make HTTP requests, download files, query SQLite, and inspect JSON and CSV |
| Checkpoints | Snapshot, compare and restore workspace files |
| Tasks and notes | Keep a persistent task list and notes for the workspace |
| Docker (opt-in) | Work with containers and Compose, subject to workspace restrictions |

Git and other installed CLIs are available through command tools. Browser and code-navigation
tools need the `[browser]` and `[code]` extras; the installer includes both via `[all]`.
Docker is disabled by default and requires `--enable docker`; see the security implications below.

For the current tool list and parameters, use `dev-link tools` or the generated
[tool reference](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/tools.md).
You can also [add tool groups through plugins](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/extending.md).

## How it works

```text
Editor / CLI agent ─────── local HTTP ──────────┐
                                                │
Web AI client ── HTTPS ── ngrok / cloudflared ──┤
                                                ▼
                                    dev-link on your machine
                                                │
                            ┌───────────────────┼───────────────────┐
                            ▼                   ▼                   ▼
                       Project files     Commands, processes   Dashboard
                       and checkpoints   and browser tools     and audit log
```

The AI stays in your client. The project and tools run on your machine; tool inputs and results
travel between them. Connecting a cloud AI client does **not** keep project data entirely local:
file contents and command output returned by tools can be sent to that client.

The Linux sandbox applies to commands. Some tools, including HTTP and browser tools, run in
the server process with their own restrictions. See the
[security model](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/security.md) for the boundaries.

## Tunnels

`dev-link start --tunnel` selects the provider from your configuration:

| | ngrok | Cloudflare quick tunnel |
| --- | --- | --- |
| Setup | ngrok installed, account authenticated, and `tunnel.ngrok_url` configured | cloudflared installed; no account needed |
| Public address | Your assigned or configured ngrok domain | A temporary `trycloudflare.com` address |
| After a restart | Same public URL while the domain and saved token remain unchanged | New public URL; reconnect your client |
| Useful for | Returning to the same workspace regularly | Temporary development sessions |

With `tunnel.ngrok_url` set, dev-link uses ngrok. Otherwise it uses cloudflared.
Installing ngrok alone does not select it. The installer can configure the domain for you;
for manual setup, see [a URL that stays the same](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md#a-url-that-stays-the-same).

For ngrok, dev-link saves a token per workspace so the public URL survives restarts. To replace
that saved token, run `dev-link restart --tunnel --new-token`, then reconnect your clients. Explicitly configured tokens must be changed in their configuration instead.

Local starts and Cloudflare quick tunnels generate a new token by default. For a stable local
connection, see [pinning the token and port](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/local.md#the-url-changes-on-restart).

Provider limits still apply. Check [ngrok's plan limits](https://ngrok.com/docs/pricing-limits/free-plan-limits/)
and [Cloudflare's quick tunnel documentation](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
for current terms. Use the local dashboard URL to avoid ngrok's browser warning page.

## Everyday commands

Run these from your project folder, or pass the folder explicitly, for example
`dev-link status ~/code/my-project`.

```bash
dev-link status             # Check the server
dev-link url                # Print the MCP URL
dev-link dashboard          # Print the local dashboard URL
dev-link logs server        # Read server logs
dev-link logs tunnel        # Read tunnel logs
dev-link doctor             # Diagnose dependencies and sandbox setup
dev-link restart --tunnel   # Restart with a public tunnel
dev-link stop               # Stop the server and its tunnel
```

Use `dev-link --help` for all commands and
[Configuration](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/configuration.md)
for tool selection, environment variables, timeouts and sandbox settings.

## Security

dev-link gives an AI the ability to modify files and execute commands. Connect clients you trust
and choose the workspace carefully.

- **Protect the token.** MCP and dashboard URLs contain the same secret in the default setup.
  Do not post them, commit them or include them in screenshots.
- **The workspace is writable.** The agent can read, change or delete its contents, including
  `.env` files and `.git`. Checkpoints are useful recovery points, not a replacement for backups.
- **Linux commands are sandboxed by default.** bubblewrap exposes the workspace, read-only
  system and toolchain paths, and a private home. Extra mounts can widen access. The sandbox
  does not make arbitrary commands harmless.
- **Network access is on by default.** Tools can reach the internet, your LAN and localhost
  services. `--no-network` restricts network access, including browser and HTTP tools;
  package installs and workflows that need network access will fail.
- **Public tunnels expose the server.** ngrok or Cloudflare terminates TLS and handles the
  traffic, including the token. Stop the server when it is no longer needed.
- **Some modes grant much broader access.** `--no-sandbox` runs commands with your user access.
  Enabling Docker with a rootful daemon is effectively root-equivalent access.
- **Logging is visibility, not prevention.** The dashboard and audit log record activity;
  they do not approve or block a harmful command.

Read the full [security model](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/security.md)
for restrictions and known limits. Report vulnerabilities privately through
[SECURITY.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/SECURITY.md).

## Platforms

| Platform | Support |
| --- | --- |
| Linux | Supported, with bubblewrap sandboxing by default |
| macOS | Experimental; requires `--no-sandbox`. Not currently tested in CI; process port detection has known limitations |
| Windows | Not supported |

The package requires Python 3.11 or newer. The installer uses uv to provision Python 3.13.

## Manual installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first. On Debian or Ubuntu:

```bash
sudo apt install bubblewrap git ripgrep
uv tool install --python 3.13 'dev-link-mcp[all] @ git+https://github.com/ajeetkharel/dev-link-mcp'
dev-link install-browser
dev-link doctor
```

On other Linux distributions, use the equivalent system packages. macOS needs git and ripgrep,
but cannot use the bubblewrap sandbox. Browser tools also need Chromium's system libraries;
the installer handles these on supported Debian/Ubuntu systems.

For web clients, install and configure [ngrok](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md#a-url-that-stays-the-same)
or [cloudflared](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/web.md#install-cloudflared).
Local clients do not need a tunnel.

## Troubleshooting

| Symptom | First thing to check |
| --- | --- |
| Server refuses to start | Run `dev-link doctor` and `dev-link logs server`. For Ubuntu sandbox restrictions, see the [setup notes](https://github.com/ajeetkharel/dev-link-mcp/blob/main/CONTRIBUTING.md#setup) |
| Client stops connecting after a restart | Run `dev-link url` and compare it with the saved URL. See [Tunnels](#tunnels) for stable URLs |
| `--tunnel` asks for cloudflared when ngrok is installed | Configure `tunnel.ngrok_url`; installing ngrok alone does not select it |
| Public connection fails | Check `dev-link logs tunnel`, your tunnel configuration and provider limits |
| Browser tools cannot start | Run `dev-link install-browser` and `dev-link doctor`; check for missing Chromium system libraries |
| A long command times out in the client | Check the client's tool-call timeout as well as dev-link's command timeout; see [local clients](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/local.md) |

If you report a bug, include the failing command, platform and relevant logs, with tokens and
project secrets removed.

## Documentation

| Guide | Contents |
| --- | --- |
| [Client setup](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/clients/README.md) | Web clients, editors, CLI agents and authentication |
| [Configuration](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/configuration.md) | Settings, environment variables and tool selection |
| [Tool reference](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/tools.md) | Available tools and parameters, generated from the code |
| [Architecture](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/architecture.md) | Tool execution, components and state |
| [Security](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/security.md) | Sandbox boundaries, authentication and known limits |
| [Extending](https://github.com/ajeetkharel/dev-link-mcp/blob/main/docs/extending.md) | Build and register your own tool groups |
| [Changelog](https://github.com/ajeetkharel/dev-link-mcp/blob/main/CHANGELOG.md) | Release history and unreleased changes |

## Contributing

Bug reports, documentation improvements and focused pull requests are welcome. Start with
[CONTRIBUTING.md](https://github.com/ajeetkharel/dev-link-mcp/blob/main/CONTRIBUTING.md)
for setup and checks. For larger changes, open an
[issue](https://github.com/ajeetkharel/dev-link-mcp/issues) first to discuss the approach.
Contributors follow the [Code of Conduct](https://github.com/ajeetkharel/dev-link-mcp/blob/main/CODE_OF_CONDUCT.md).

## License

[MIT](https://github.com/ajeetkharel/dev-link-mcp/blob/main/LICENSE).
