# Authentication and keys

This section is for people and programs that call models through Lumewarp. You need two
things from whoever runs it: the gateway's base URL, and an API key (`lw-…`), which you can
usually create yourself ([Your keys](../self-service/index.md)).

## Use any OpenAI client

Point the client at the gateway and use your Lumewarp key as the API key:

```python
from openai import OpenAI

client = OpenAI(base_url="https://gateway.example.com/v1", api_key="lw-...")

reply = client.chat.completions.create(
    model="demo",
    messages=[{"role": "user", "content": "Hello"}],
)
print(reply.choices[0].message.content)
```

```sh
curl -sS https://gateway.example.com/v1/chat/completions \
  -H "Authorization: Bearer lw-..." -H "Content-Type: application/json" \
  --data-binary '{"model":"demo","messages":[{"role":"user","content":"Hello"}]}'
```

`model` is a name your key is granted. It may be a model, an alias or a router; you do not
need to know which.

## Which names can I call?

```sh
curl -sS https://gateway.example.com/v1/models -H "Authorization: Bearer lw-..."
```

The list contains exactly the names your key may call, each with its `type` (`llm`,
`embedding`, `transcription` or `decision`). The Admin UI shows the same list on **My keys**,
with your limits and remaining budget.

## Key states

| State | The gateway answers | What to do |
| --- | --- | --- |
| Active | Serves the request | |
| Expired, suspended or revoked | 401 `invalid_api_key` | Resume it or create a new one under **My keys**; a revoked key cannot come back. |
| Budget exhausted | 429 `insufficient_quota` | Wait for the next month or ask a team admin to raise the budget. |

A change to a key reaches the gateways within a few seconds.

## Rotating without downtime

Rotating a key replaces its secret. With a grace period, the old secret keeps working until
the grace ends (at most 24 hours), so you can deploy the new one first. Both secrets are
the same key: usage and limits stay on it.

## MCP and A2A use another header

The MCP and A2A endpoints take the same key in `X-Lumewarp-Key` instead of
`Authorization`. See [MCP](./mcp.md) and [A2A](./a2a.md).
