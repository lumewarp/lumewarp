# MCP

The gateway proxies the MCP servers an administrator registered. It speaks MCP revision
2025-11-25 over Streamable HTTP, and passes through an upstream that negotiates 2025-06-18.

## Two endpoints

| Endpoint | What it is |
| --- | --- |
| `POST /mcp` | One virtual server made of every MCP server your key is granted. Tools and prompts are named `<server>__<name>`. |
| `POST /mcp/servers/<server>` | One server, with its tool and prompt names unchanged. |

Configure an MCP client with either URL and this header:

```
X-Lumewarp-Key: lw-...
```

Send the key in exactly one `X-Lumewarp-Key` header. `Authorization: Bearer` does not
authenticate these endpoints.

## What you see

- A list (`tools/list`, `prompts/list`) contains only what your key is granted. A server or
  tool you are not granted is invisible, and calling it is refused.
- An administrator can require tool and prompt definitions to be reviewed. A definition
  that is new or has changed may be withheld until it is approved, so a tool can disappear
  from the list when its upstream changes its description or schema.
- `tools/call` counts as one request against your key's requests-per-minute limit. Tokens,
  concurrency and budgets are not charged, and a key whose budget is exhausted still works
  here.

## Sessions

`initialize` returns an `Mcp-Session-Id`; send it on every later request, as MCP requires.
A session lasts 24 hours and works on any gateway of the installation. When the gateway
answers 404 for a session, initialize again.

The aggregate endpoint has no server-to-client stream (`GET /mcp` answers 405), so it sends
no `list_changed` notifications: list again when you need a fresh view. The single-server
endpoint forwards the upstream's stream.

## Limits and errors

- A request body over 4 MiB is refused with 413.
- A response over 16 MiB, or a single stream event over 1 MiB, fails with 502.
- Protocol errors are JSON-RPC errors in a 200 response. Transport errors (a missing
  header, an unknown server, an expired session) use HTTP statuses with the code `mcp_error`.
- Not supported: resources and subscriptions, and MCP tasks.
