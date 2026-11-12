# Plugin development skill

The repository ships a [Claude Code](https://docs.claude.com/en/docs/claude-code) skill,
`lumewarp-plugin-dev`, that takes a plugin from "which kind do I need" to a verified install.
Its templates are useful without Claude Code too: each one is a complete project that
builds and passes its tests in a repository of your own.

## What is in it

[`.claude/skills/lumewarp-plugin-dev/`](https://github.com/lumewarp/lumewarp/tree/main/.claude/skills/lumewarp-plugin-dev)

| Path | |
| --- | --- |
| `SKILL.md` | The workflow: choose the extension point (and what no plugin can do), copy a template, implement, test, build, verify the load, install. |
| `templates/router-strategy` | A router strategy with per-router and per-member settings, tests through the gateway's own configuration path, and a standalone YAML to send a request through it. |
| `templates/upstream-auth` | OAuth 2.0 client credentials for an MCP server or A2A agent: `http-fetch`, `secret()` and a cached bearer token. |
| `templates/a2a-middleware` | Stamps a metadata value into A2A message requests. |
| `templates/rust-toolchain.toml` | Adds the `wasm32-wasip2` target; copy it next to a WASM template's `Cargo.toml`. |
| `templates/python-auth` | A `lumewarp.auth` plugin that admits only listed email domains. |
| `templates/python-authz` | A `lumewarp.authz` backend that reads grants from a directory service, tested with the control plane's conformance suite. |
| `references/` | Links to this manual's [gateway plugins](../plugins.md), [router strategies](../router-strategies.md) and [Python plugins](./python-plugins.md) pages. |

The WASM templates depend on `lumewarp-plugin-sdk` from this repository by git tag, and the
Python templates on `lumewarp-control-plane` the same way (as a development dependency
only). Change the tag to the release you deploy.

## Using it

Inside a clone of this repository Claude Code finds the skill on its own. To use it in
your own plugin repository, copy it into that project's skills directory, or into your
personal one, resolving the reference links:

```sh
cp -rL lumewarp/.claude/skills/lumewarp-plugin-dev ~/.claude/skills/
```

Then describe what the plugin should do, for example "a router strategy that prefers the
member with the most free KV cache, but never one over 90 %". The skill picks the kind,
starts from the matching template and runs the tests and the load check.

Without Claude Code, copy a template directory and follow `SKILL.md` by hand. The short
version for a WASM plugin:

```sh
cp -r lumewarp/.claude/skills/lumewarp-plugin-dev/templates/router-strategy my-strategy
cp lumewarp/.claude/skills/lumewarp-plugin-dev/templates/rust-toolchain.toml my-strategy/
cd my-strategy
cargo test
cargo build --release --target wasm32-wasip2
mkdir -p plugins/router && cp target/wasm32-wasip2/release/*.wasm plugins/router/
docker run --rm -v "$PWD/plugins:/plugins:ro" \
  -e LUMEWARP_ROUTER_PLUGIN_DIR=/plugins/router \
  ghcr.io/lumewarp/lumewarp-gateway:<release> plugins
```

and for a Python plugin, `uv run pytest` in the copied directory.

To port a router policy you already run in Python, the `port-router-to-rust` skill in the
same directory adds a method for comparing the two implementations and a worked example.
