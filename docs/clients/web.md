# Web clients

ChatGPT and Grok run in the vendor's cloud, not on your machine. They connect only to MCP servers
they can reach from the internet over HTTPS, so dev-link has to be published through a tunnel.

**Read this first.** Anyone with the public URL can run commands on your machine, inside the
sandbox described in [security.md](../security.md). With `--no-sandbox`, which macOS needs, they
get your full user access instead. The URL holds the token, so share it only with the client,
and stop the server when you are done.

## Install cloudflared

`--tunnel` starts a Cloudflare quick tunnel, which needs the `cloudflared` binary but no
Cloudflare account. The [install script](../../scripts/install.sh) puts it in `~/.local/bin` on
Linux. To install it from Cloudflare's package repository instead:

Debian and Ubuntu:

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
```

RHEL:

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflared.repo | sudo tee /etc/yum.repos.d/cloudflared.repo
sudo yum install cloudflared
```

macOS: `brew install cloudflared`. Other systems: see Cloudflare's
[downloads page](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/).
`dev-link doctor` shows whether cloudflared is found.

## Start a public server

```bash
dev-link start DIR --tunnel
dev-link url DIR                # https://<random>.trycloudflare.com/mcp/<token>
```

Paste that URL into the client as the server URL, with no authentication: the token in the path
is the secret. Neither client documents a field for a fixed header, so `bearer` auth does not
work here.

## ChatGPT

1. Open [chatgpt.com/plugins](https://chatgpt.com/plugins) on the web, choose the plus button,
   then **Add custom MCP server**.
2. Enter a name. Under **Connection**, paste the URL from `dev-link url` as the server URL.
3. Choose **No authentication**.
4. Read the risk warning, choose **I understand and want to continue**, then
   **Create as a plugin**.
5. Install the plugin, then pick it in a new chat.

ChatGPT asks you to confirm each call to a tool that writes. dev-link marks the tools that only
read as read-only, so ChatGPT treats them as reads.

Which plans can add custom MCP servers, and which allow tools that write, has changed several
times, and workspace admins can turn the feature off. Check OpenAI's help article
["Developer mode and MCP apps in ChatGPT"](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
for your plan.

## Grok

1. Open [grok.com/connectors](https://grok.com/connectors), choose **New Connector**, then
   **Custom**.
2. Enter the URL from `dev-link url` as the MCP server URL. dev-link needs no sign-in step.

On Grok Business and Enterprise, a team admin has to add the connector in the xAI console before
members can use it. See xAI's [connector docs](https://docs.x.ai/grok/connectors).

## After a restart

The tunnel hostname and the token both change on every start, so the old URL stops working.
Remove the server from the client and add it again with the new URL from `dev-link url DIR`.

A URL that stays the same needs your own tunnel or reverse proxy with a fixed hostname: start with
`--public-url https://your.host` and a pinned token (see
[The URL changes on restart](local.md#the-url-changes-on-restart)).

## Stopping and troubleshooting

`dev-link stop DIR` stops the server and the tunnel. If cloudflared exits on its own, the server
stops too, and `dev-link logs server DIR` shows why.

Cloudflare gives quick tunnels no uptime guarantee and allows up to 200 requests in flight per
tunnel. They suit a working session, not a server that stays up.

The client steps on this page were checked against each vendor's documentation on 2026-10-10.
Web clients change their menus often; if a step no longer matches, their docs win.
