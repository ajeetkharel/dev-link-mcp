# Connecting clients

dev-link speaks MCP over Streamable HTTP only; there is no stdio mode. Every client connects to
the URL that `dev-link url` prints, and the token in that URL is the password (see
[security.md](../security.md)).

The commands on these pages are run in the project folder. Every `dev-link` command also takes
the folder as an optional last argument, such as `dev-link url ~/code/my-app`, for running it
from somewhere else.

How you connect depends on where the client runs:

| Client | Runs | Page |
| --- | --- | --- |
| ChatGPT | in OpenAI's cloud | [web.md](web.md#chatgpt) |
| Grok | in xAI's cloud | [web.md](web.md#grok) |
| Claude Code | on your machine | [local.md](local.md#claude-code) |
| Codex CLI | on your machine | [local.md](local.md#codex-cli) |
| Cursor | on your machine | [local.md](local.md#cursor) |
| VS Code | on your machine | [local.md](local.md#vs-code) |

- [Web clients](web.md) need a public HTTPS URL, so dev-link starts a Cloudflare tunnel with
  `--tunnel`. That publishes the server to the internet; read the warning at the top of the page
  first.
- [Local clients](local.md) connect to `http://127.0.0.1`. The page also covers keeping the URL
  the same across restarts and `bearer` auth.

Any other MCP client that supports Streamable HTTP works the same way: a client on your machine
follows the local steps, a client in the cloud follows the web steps.
