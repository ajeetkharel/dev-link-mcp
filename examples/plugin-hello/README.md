# dev-link-hello

Example dev-link plugin that adds one tool, `hello`. Install it next to dev-link
(`pip install ./examples/plugin-hello`, or `uv tool install dev-link-mcp --with ./examples/plugin-hello`),
then enable it by name, e.g. in `~/.config/dev-link/config.yaml`:

    tools:
      enabled: [hello]
    groups:
      hello:
        greeting: Hi
