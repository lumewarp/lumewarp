# Extension points

Lumewarp can be extended in five places. Which one you need depends on where the decision
has to be made.

| You want to | Extension point | Runs in | Written in |
| --- | --- | --- | --- |
| Choose which member model serves a request | [Router strategy](../router-strategies.md) | Gateway, on the request path | Rust → WASM |
| Compute the credential sent to an MCP server or A2A agent | [Upstream auth](../plugins.md#upstream-auth) | Gateway | Rust → WASM |
| Inspect or change A2A messages | [A2A middleware](../plugins.md#a2a-middleware) | Gateway | Rust → WASM |
| Decide who a signed-in identity is | [`lumewarp.auth`](./python-plugins.md) | Control plane | Python |
| Answer "is this user an admin or member of this team?" from your own system | [`lumewarp.authz`](./python-plugins.md#writing-an-authorization-backend) | Control plane | Python |

## Two rules behind the split

**Python never runs on the request path.** A control plane plugin runs when someone signs
in or when an administrator makes a change, and its result is compiled into the snapshot.
It cannot decide anything per request.

**Gateway plugins are sandboxed WASM components.** They are loaded once at startup from a
directory per kind, have no network, file or clock access beyond what the host passes in,
and are bounded in time and memory. They declare their settings as a JSON schema, which the
control plane validates on write and the Admin UI renders as a form.

## What a plugin cannot do

- Translate to a non-OpenAI provider API. Upstreams are OpenAI-compatible only.
- Change retries, fallbacks, traffic splits, stickiness or circuit breaking. Those belong to
  the gateway; a router strategy only picks among the candidates it is given.
- Call an external service from a router strategy. Everything a strategy reads is in memory.
- Be replaced while the gateway runs. A new `.wasm` file needs a gateway restart.

## Writing one

- Start from the examples in
  [`gateway/plugins/`](https://github.com/lumewarp/lumewarp/tree/main/gateway/plugins).
- A Claude Code skill that walks through the whole loop is planned; until then
  `.claude/skills/port-router-to-rust` in the repository covers porting a Python router.
