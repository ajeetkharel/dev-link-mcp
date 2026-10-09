# Contributing

Bug reports, fixes and new tools are welcome. For a larger change, open an issue first so we can
agree on the approach before you write it. Security problems go through private reporting, see
[SECURITY.md](SECURITY.md). Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Setup

dev-link runs on Linux, and on macOS with `--no-sandbox`. Windows is not supported. It needs
Python 3.11 to 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras               # the dev group already pulls in [all]; this makes it explicit
uv run dev-link install-browser    # Chromium for the browser tools and tests
uv run dev-link doctor             # what is missing on this machine, and how to fix it
```

The sandbox needs bubblewrap (`apt install bubblewrap`, or your distribution's package). On
Ubuntu 23.10 and later AppArmor blocks the unprivileged user namespaces bwrap needs; see your
distribution's docs on `kernel.apparmor_restrict_unprivileged_userns` or an AppArmor profile for
bwrap. `dev-link doctor` tells you when this is the problem.

## Checks

These must pass before a pull request is merged; CI runs the same commands:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run python scripts/gen_docs.py --check
uv run pytest
```

Some tests need something from the host and carry a marker for it: `bwrap`, `browser`, `docker`
or `network` (registered in `pyproject.toml`, probed in `tests/conftest.py`). When the host lacks
it, the test is skipped with the reason; `uv run pytest -rs` lists them. Select or leave out a
group with `-m`, for example `uv run pytest -m "not docker"` or `uv run pytest -m browser`.

One test runs a command over a 64 MB memory limit on purpose, and the kernel kills it inside its
own systemd scope. GNOME reports any such kill as "Device memory is nearly full", even though
nothing else on the machine is affected.

`docs/configuration.md`, `docs/config.schema.json` and `docs/tools.md` are generated. After
changing a setting, a tool, a tool description or a parameter, run
`uv run python scripts/gen_docs.py` and commit the result. CI fails when they are out of date.

## Where things go

Add a tool to an existing group: add an `@mcp.tool` function inside the group's `register()` in
`src/dev_link_mcp/tools/<group>.py` (the `project` group lives in `tools/project/__init__.py`).
Test it in `tests/test_<group>.py`; some test files hold the group's expected tool names, such
as `TOOLS` in `tests/test_project.py`. If the group has an entry with a `tools` tuple in
`BUILTINS`, add the name there too. Then regenerate the docs.

Add a built-in group: subclass `ToolGroup` from `src/dev_link_mcp/tools/base.py` in a new module
under `tools/`, and add a `Builtin` row to `BUILTINS` in `src/dev_link_mcp/tools/__init__.py`.
The order of `BUILTINS` is the registration order. A group that needs extra packages lists their
import names in `requires`, its tool names in `tools`, and gets a pyproject extra with the same
name (`tests/test_registry.py` checks this). Add `tests/test_<group>.py` and regenerate the docs.

Add a test-output parser: write `_parse_<runner>(lines) -> SuiteSummary | None` in
`src/dev_link_mcp/tools/project/parsers.py`, add it to `_PARSERS` (first match wins, so more
specific formats go first), and add a sample to `SAMPLES` in `tests/test_project.py`.

Add a project detector: write `_detect_<stack>(root, det)` in
`src/dev_link_mcp/tools/project/detect.py`, fill `det` with `det.add(...)` and `det.source(...)`
using the command kinds in `KINDS`, and add it to `_DETECTORS` (earlier detectors win for the
same command kind). Add a `test_detect_*` case in `tests/test_project.py`.

Add a sandbox backend: write a class in `src/dev_link_mcp/sandbox/backends/` that follows the
`Backend` protocol in `backends/__init__.py` (`name`, `confined`, `available`, `visible`,
`wrap`), choose it in `select()` there, and add its name to `SandboxBackend` in
`src/dev_link_mcp/config/models.py`. Test it in `tests/test_sandbox_backends.py`.

Tool groups from other packages are plugins; see [docs/extending.md](docs/extending.md) and
[examples/plugin-hello](examples/plugin-hello). The plugin API and the config format are
provisional while the version is 0.x, so any change to them needs a CHANGELOG entry.

## Commits and pull requests

Write commit subjects in the imperative and say what changed, for example "Prune audit events and
process logs older than audit.retention_days at startup" or "Kill the sandbox's namespace init
when stopping a just-started command". Use the body to explain why, and give numbers when the
change is about speed or memory. No ticket keys and no co-author tags. Keep one topic per commit.

A pull request should:

- include tests for the change, or say why it has none,
- pass the checks above,
- include regenerated docs when settings or tools changed,
- add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.

By contributing you agree that your work is licensed under the [MIT License](LICENSE).
