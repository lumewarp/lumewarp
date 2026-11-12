# A2A

The gateway proxies the A2A agents an administrator registered, using A2A 1.0 over
JSON-RPC and SSE.

## Endpoints

| Endpoint | What it is |
| --- | --- |
| `GET /a2a` | The agents your key is granted, each with the URL of its card. |
| `GET /a2a/<agent>/.well-known/agent-card.json` | The agent's card. |
| `POST /a2a/<agent>` | JSON-RPC calls to the agent, including `SendStreamingMessage` and `SubscribeToTask`. |

Every request carries the key in exactly one header, and every JSON-RPC request the
protocol version:

```
X-Lumewarp-Key: lw-...
A2A-Version: 1.0
```

A missing or different `A2A-Version` is the JSON-RPC error `-32009`.

## The card is the gateway's

The card you fetch is rewritten so that a standard A2A client talks to the gateway, not to
the agent directly: it lists one JSON-RPC interface at the gateway's public URL, declares
the `X-Lumewarp-Key` security scheme, carries no signatures and states that push
notifications are unavailable.

## Tasks belong to the key

Task and context ids are opaque values starting with `lwt.`. They belong to the key that
received them: another key, even one of the same person, cannot use them.

## Not supported

- Push notification configuration is refused.
- `ListTasks` works only for agents the administrator set to forward your own
  `Authorization` header (`passthrough`); otherwise it is the JSON-RPC error `-32004`.
- A2A requests are not rate limited and not billed.
