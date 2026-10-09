# Connecting clients

dev-link speaks MCP over Streamable HTTP only; there is no stdio mode. Start a server and print
its URL:

```bash
dev-link start DIR
dev-link url DIR        # http://127.0.0.1:8765/mcp/<token>
```

Every client below takes that URL as a remote HTTP server. The token in the path is the
password: anyone with the URL can run commands in DIR (see [security.md](security.md)).

## The URL changes on restart

A new token is generated on every start, and `dev-link start` takes the next free port when 8765
is busy. A URL saved in a client config stops working after a restart. You have three options:

- Re-add the server after each start. This is the safest, because the old URL dies with the old
  server.
- Pin the token and port, so the URL stays the same:

  ```bash
  export DEV_LINK_SERVER__TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  dev-link start DIR --port 8765
  ```

  The same works with `server.token` and `server.port` in `~/.config/dev-link/config.yaml`. A
  pinned token never expires, so keep it out of anything you commit.
- Use `bearer` auth (`dev-link start DIR --auth bearer`). The URL becomes
  `http://127.0.0.1:<port>/mcp` and the token goes in an `Authorization: Bearer <token>` header.
  `dev-link start` prints the header line. This suits clients that can read the header from an
  environment variable, so the token stays out of the config file.

`dev-link url DIR` always prints the current URL, and `dev-link status DIR` shows whether the
server runs.

## Claude Code

```bash
claude mcp add --transport http dev-link "$(dev-link url DIR)"
```

Options come before the name and URL. `--scope` picks where the entry goes:

- `local` (default): `~/.claude.json`, for the current project only.
- `user`: `~/.claude.json`, for all your projects.
- `project`: `.mcp.json` in the project root, meant to be committed. Do not put a token there.
  `.mcp.json` expands `${VAR}` in `url` and `headers`, so a committed file can read the URL
  from your environment instead (set `DEVLINK_URL` to the output of `dev-link url DIR`; names
  starting with `DEV_LINK_` are read by dev-link itself):

  ```json
  {
    "mcpServers": {
      "dev-link": { "type": "http", "url": "${DEVLINK_URL}" }
    }
  }
  ```

With `bearer` auth, add the header after the URL:

```bash
claude mcp add --transport http dev-link http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

Check it with `claude mcp list` or `claude mcp get dev-link` (look for `Connected`), or `/mcp`
inside a session. After a restart with a new token, run `claude mcp remove dev-link` and add it
again. Claude Code's tool timeout can be raised with `MCP_TOOL_TIMEOUT` (milliseconds) if
long-running commands time out on the client side.

## Codex CLI

Add the server to `~/.codex/config.toml` (or `.codex/config.toml` in a trusted project):

```toml
[mcp_servers.dev-link]
url = "http://127.0.0.1:8765/mcp/<token>"
tool_timeout_sec = 600
```

or from the command line:

```bash
codex mcp add dev-link --url "$(dev-link url DIR)"
```

Codex waits 60 s for a tool call by default, less than dev-link's default command timeout of
120 s, hence `tool_timeout_sec` above.

With `bearer` auth, Codex reads the token from an environment variable. Pinning the token with
`DEV_LINK_SERVER__TOKEN` lets both sides use the same variable:

```toml
[mcp_servers.dev-link]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "DEV_LINK_SERVER__TOKEN"
```

`codex mcp add dev-link --url http://127.0.0.1:8765/mcp --bearer-token-env-var DEV_LINK_SERVER__TOKEN`
writes the same entry. Check it with `codex mcp list`, or `/mcp` in the TUI.

## Cursor

Put the server in `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` in the project root:

```json
{
  "mcpServers": {
    "dev-link": { "url": "http://127.0.0.1:8765/mcp/<token>" }
  }
}
```

Cursor expands `${env:NAME}` in `url` and `headers`, so the file can hold
`"url": "${env:DEVLINK_URL}"` instead of the token. With `bearer` auth:

```json
{
  "mcpServers": {
    "dev-link": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${env:DEV_LINK_SERVER__TOKEN}" }
    }
  }
}
```

Check that the dev-link tools appear under Customize or in chat. Connection errors are in the
Output panel under "MCP Logs".

## VS Code

Put the server in `.vscode/mcp.json` in the workspace, or in your user `mcp.json` (run
**MCP: Open User Configuration** from the Command Palette). VS Code uses a `servers` key and
`"type": "http"`:

```json
{
  "servers": {
    "dev-link": { "type": "http", "url": "http://127.0.0.1:8765/mcp/<token>" }
  }
}
```

With `bearer` auth, let VS Code prompt for the token instead of storing it:

```json
{
  "inputs": [
    { "type": "promptString", "id": "dev-link-token", "description": "dev-link token", "password": true }
  ],
  "servers": {
    "dev-link": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${input:dev-link-token}" }
    }
  }
}
```

Check it with **MCP: List Servers**, the server's **Show Output** log, or the tools picker in
chat.

## ChatGPT

ChatGPT connects only to MCP servers it can reach from the internet over HTTPS, so publish the
server through a tunnel:

```bash
dev-link start DIR --tunnel     # needs cloudflared
dev-link url DIR                # https://<random>.trycloudflare.com/mcp/<token>
```

In ChatGPT on the web, open Plugins, choose the plus button, then **Add custom MCP server**.
Enter a name, paste the URL from `dev-link url` as the server URL, and choose **No
authentication**: the token in the path is the secret. ChatGPT offers OAuth or no
authentication, not a fixed header, so `bearer` auth does not work here.

Both the tunnel hostname and the token change on every start, so the server has to be added
again after each restart. A fixed hostname needs your own tunnel or proxy: start with
`--public-url https://your.host` and a pinned token.

Anyone with the public URL can run commands on your machine. Stop the server
(`dev-link stop DIR`) when you are done.

Which ChatGPT plans can add custom MCP servers, and whether tools that write are allowed on
each, has changed several times. Workspace admins can also turn the feature off. Check OpenAI's
help article "Developer mode and MCP apps in ChatGPT" for your plan.

The client settings on this page were checked against each client's documentation on
2026-10-09. Clients change often; if a step no longer matches, their docs win.
