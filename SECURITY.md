# Security policy

## Supported versions

Only the latest 0.x release gets security fixes. Fixes are not backported to older releases.

## Reporting a vulnerability

Report it privately through GitHub:
[Report a vulnerability](https://github.com/ajeetkharel/dev-link-mcp/security/advisories/new).
Please do not open a public issue, discussion or pull request for it.

Include what you can of:

- the dev-link version (`dev-link --version`) and `dev-link doctor` output,
- OS, distribution and kernel version, and whether the sandbox was on,
- the config or flags you used (remove any tokens),
- steps or a proof of concept that shows the problem, and what an attacker gains.

## What to expect

This is a project with one maintainer. I aim to acknowledge a report within 7 days and to tell
you whether I can reproduce it within 14. A fix and an advisory follow when they are ready; I
will agree a disclosure date with you and credit you unless you ask me not to.

## Scope

The threat model and its accepted risks are in [docs/security.md](docs/security.md).

In scope, for example:

- a command escaping the bubblewrap sandbox: reading or writing outside the workspace and the
  paths the sandbox config exposes,
- reaching the MCP endpoint or the dashboard without the token, or the token leaking from
  dev-link itself (logs, dashboard, process listings),
- tools writing outside the workspace when the sandbox is on, or the dashboard serving files it
  should refuse.

Not vulnerabilities, because `docs/security.md` documents them as accepted:

- full user access with `--no-sandbox` (`sandbox.backend: none`),
- the docker group being root-equivalent on a host where the user can run docker,
- plugins running as trusted code with the server's access,
- someone who has the URL, for example a leaked tunnel URL, being able to use the server.
