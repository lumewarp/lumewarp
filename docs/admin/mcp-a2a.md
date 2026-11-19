# MCP servers and A2A agents

An installation admin registers MCP servers and A2A agents in a chosen zone; the same
server or agent name may exist in another zone. Team and org admins grant them through the
global policy that grants models, but a key can use only the resources compiled into its own
zone.

## MCP servers

A server belongs to one zone and has a name (used in `/mcp/servers/<name>` and as the
`<name>__` prefix of its tools), a URL, outbound authentication and a review mode. Its
upstream URL and gateway probes are specific to that zone.

**Outbound authentication** is one of:

| `auth.plugin` | The gateway sends |
| --- | --- |
| `none` | Nothing. |
| `static_bearer` | One bearer token: entered here and stored sealed, or read from a gateway environment variable. |
| `passthrough` | The caller's own `Authorization` header, unchanged. |
| a WASM plugin | Whatever the plugin computes; see [gateway plugins](../plugins.md). |

**Review mode** decides which tool and prompt definitions clients see:

| Mode | A definition seen for the first time | A known definition that changed |
| --- | --- | --- |
| `tofu` (default) | Served, and recorded as approved | Withheld until approved |
| `manual` | Withheld until approved | Withheld until approved |
| `off` | Served | Served |

Withheld definitions appear in the server's **Definitions** tab, where you approve or
reject them. **Discover now** asks the server for its definitions from the control plane,
without waiting for a gateway to see them. The full rules are in
[MCP definition review](../plugins.md#mcp-definition-review).

## A2A agents

An agent belongs to one zone and has a name (used in `/a2a/<name>`), a URL, outbound
authentication, an optional card URL and an optional middleware plugin.

- Gateways fetch the agent's card every few minutes and report it, so the agent's page
  shows the card as clients receive it. For a `passthrough` agent the gateway has no
  credential to fetch with: paste the card instead.
- One [A2A middleware plugin](../plugins.md#a2a-middleware) per agent can inspect or change
  messages.

## Granting

A policy has three independent, installation-wide parts: models, MCP servers and A2A agents.
For each, a level either inherits its parent, allows everything the parent allows, or lists
exactly what it allows. The grant is shared across zones, but the key's zone determines
which server or agent can actually appear in its snapshot. An MCP grant covers a whole
server or named tools of it; a granted server exposes all its prompts.

## What a gateway needs

MCP and A2A answer 503 until the gateways have `LUMEWARP_GATEWAY_ID_KEY`, and A2A also
needs `LUMEWARP_GATEWAY_PUBLIC_URL`. See [Deploy with Helm](../operations/helm.md#gateway-plugins-wasm) or [Deploy with Docker](../operations/docker.md#operating-it).
