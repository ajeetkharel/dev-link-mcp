# Security

dev-link lets a remote agent run any command in a folder on your machine. This page says what
that exposes, what the sandbox holds back, and what it does not. To report a vulnerability, see
[SECURITY.md](../SECURITY.md).

## The URL is the key

The MCP URL printed by `dev-link url` contains a secret token in its path. Whoever has that URL
can run commands on this machine with the access described below. With `--tunnel` the public
URL works from anywhere on the internet. Treat both like a password.

- A new random token is generated on every start, unless you pin one with `--token` or
  `server.token`. To rotate a token, restart the server (`dev-link restart DIR`) with no pinned
  token. A pinned token stays valid until you change it.
- The dashboard URL contains the same token. In `bearer` mode the MCP endpoint wants an
  `Authorization` header, but the dashboard still uses the token in its path, because a
  browser cannot add a header to a page load.
- The token is written to `connection.json` and to the banner in `server.log` (both 0600 in the
  state dir). It is replaced with `***` in the audit log. Request logging is off, so it does not
  end up in access logs.
- The server speaks plain HTTP. On the default `127.0.0.1` that stays on your machine. If you
  bind to a LAN address with `--host`, the token crosses the network unencrypted.
- `--tunnel` runs a Cloudflare quick tunnel. TLS ends at Cloudflare, so Cloudflare handles the
  token and all traffic in clear text.
- `auth: none` is only accepted on a loopback host (`127.0.0.1`, `localhost`, `::1`). The server
  refuses to start with `none` on any other host, together with `public_url` or `allowed_hosts`,
  or with `--tunnel`. Even on loopback, every local user and process can then use the server,
  including agents of other dev-link servers on this machine whose network is on.

## Network

The network is on by default. Sandboxed commands then share the host's network namespace, so
the agent can reach:

- the internet and your LAN;
- services that listen on the host's loopback, such as local databases, admin UIs and other
  dev-link servers;
- cloud metadata endpoints such as `169.254.169.254` when the machine is a cloud VM;
- abstract Unix sockets, which belong to the network namespace. On a desktop session this
  includes the X11 display socket (`@/tmp/.X11-unix/X0`). Whether the X server accepts a
  connection depends on its access control, which dev-link does not change.

Turn it off with `--no-network` or `sandbox.network: false`. Each command then runs in its own
empty network namespace. Package installs, `git fetch`/`push` and anything else that needs the
network fail. A server started with `start_process` cannot be reached from other tool calls,
because each command has its own loopback.

Some tools do not run in the sandbox, and the network setting means something different for them:

- `http_request`, `fetch_url`, `download_file` and the browser run in the server process. With
  the network off they may only reach loopback addresses, and that is the host's loopback. So
  they can still reach services listening on `127.0.0.1` on your machine.
- The docker group ignores `sandbox.network`: containers get Docker's normal networking.

## What the bubblewrap sandbox does

Every command runs in fresh Linux namespaces (`bwrap --unshare-all --die-with-parent
--new-session`), sharing only the network when it is on. Inside it:

- The workspace is mounted read-write at its real path.
- `/usr`, `/etc`, `/opt` and `/srv` are mounted read-only, with the `/bin`, `/sbin` and `/lib*`
  links. Anything there that your user can read, the agent can read.
- The paths in `sandbox.ro_paths` are mounted read-only. The defaults are toolchain folders in
  your home, such as `~/.nvm`, `~/.cargo/bin`, `~/.rustup`, `~/go` and `~/.pyenv`. `~/go` can
  hold module sources.
- The paths in `sandbox.rw_paths` are mounted read-write. Anything you add there is the agent's.
- Your home directory is replaced by a private one in the state dir. Your real dotfiles, SSH
  keys, cloud credentials and other projects are not visible. The private home keeps the
  agent's caches between runs. Its `.gitconfig` holds only your git name and email.
- `/tmp` is a private directory, emptied at each server start. `/run` is an empty tmpfs. Other
  top-level folders such as `/var`, `/media` and `/mnt` are not mounted, apart from the parent
  folders of the mounts above.
- `/proc` belongs to a new PID namespace, so host processes and their command lines are hidden.
- The environment is rebuilt from scratch: `PATH`, `HOME`, `USER`, `LANG`, a few variables that
  turn off colours and pagers, the variables listed in `sandbox.env_passthrough`, and
  `sandbox.env`. Nothing else from the server's environment gets in.

What it does not protect against:

- Kernel bugs. bubblewrap relies on unprivileged user namespaces; an exploitable kernel bug
  there is an escape.
- Anything in the workspace. The agent can read, change and delete every file in it, including
  `.env` files, keys and the `.git` directory, and can push with any credentials it finds there.
- Secrets you hand over yourself through `sandbox.env`, `env_passthrough`, `rw_paths` or
  `ro_paths`.
- What it can reach over the network (see above).
- Resource use. Without `limits.memory_mb` and `limits.max_pids` a command can use all memory
  and processes your user can.

Tools that run on the host read files the agent wrote. Checkpoints run host `git` against the
workspace with your git config and hooks turned off. The docker group checks compose files
before it uses them. These checks are code that can have bugs.

## No sandbox

`--no-sandbox` (`sandbox.backend: none`) runs every command as your user, with your real home,
SSH keys, cloud credentials, your full environment and every other project you can open. File
tools accept any host path. This is the same trust you give a coding agent run in your own
terminal. dev-link logs a warning and the dashboard shows the mode, but nothing else is held
back.

If bubblewrap cannot run (not Linux, `bwrap` missing, user namespaces blocked), the default
`auto` backend refuses to start. It never falls back to `none` on its own.

## Docker

The docker group is off unless you enable it (`--enable docker`). The docker daemon runs as
root, so access to it is root-equivalent on the host. The docker tools run the host `docker`
CLI outside bubblewrap. They refuse privileged mode, host namespaces, extra capabilities,
devices, bind mounts outside the workspace and the docker socket, and only touch containers
labelled with this workspace. Treat those checks as a guard against mistakes, not as a security
boundary: enable docker only for an agent you would trust with root.

## Plugins

Plugin tool groups are ordinary Python packages that run inside the server process, outside the
sandbox, with your full user access. A plugin is loaded only when `tools.enabled` names it, but
from then on it is trusted code. Install plugins you would install as any other dependency.

If the Python environment that runs dev-link is inside the workspace (for example a checkout
used as its own workspace), the agent can change dev-link's code and its plugins, and the
changes run unsandboxed at the next start. Run dev-link from an environment outside the
workspace.

## Audit log and dashboard

Every tool call is recorded with its arguments, its result (up to `audit.result_bytes`, default
256 kB) and any error, in `audit.sqlite3` and `audit.jsonl` in the state dir. Background process
output goes to `processes/` there. The dashboard shows all of it, and its Files tab shows every
file in the workspace except `.git` internals.

What is redacted:

- the server token, wherever it appears in arguments, results and errors, and in process logs
  shown on the dashboard;
- string values of argument keys whose name contains `secret`, `token`, `password`, `passwd`,
  `api_key`, `authorization`, `cookie` or `credential`.

Everything else is stored as is: a password inside a shell command, file contents the agent
read, command output, the values of `sandbox.env` when a command prints them. Process log
files on disk are not redacted. Anyone who can read the state dir or open the dashboard can see them.

Events and process logs older than `audit.retention_days` (default 30) are deleted when the
server starts. A server that runs for weeks keeps everything until its next start. `null` keeps
everything forever. The state dir is created with mode 0700. If you point `state_dir` at a
folder the sandbox can see (for example under `/opt` or `/srv`), the agent can read the audit
log and the token; keep it under your home.

## Config inside the workspace is never read

The agent can write anywhere in the workspace. A config file there could turn the sandbox off
or add `rw_paths` for the next start, so dev-link never looks for config files in the workspace
and refuses a `--config` or user file whose path is inside it. For the same reason the state
dir must be outside the workspace, so the agent cannot rewrite its own audit log.

## Reporting a vulnerability

Please report security problems privately as described in [SECURITY.md](../SECURITY.md), not in
public issues.
