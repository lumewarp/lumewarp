# Requests

**Requests** is the request log: one row per gateway request, newest first. Usage
aggregates the same events; the audit log records administrative changes only.

![The Requests page](/screenshots/requests.png)

The page shows one row per request: **Date**, **Requested** (the name the client called),
**Resolved** (the model that served it; hover for the alias, router, fallback and upstream
it went through, and a badge shows the outcome of a request that did not complete),
**Key** (with a Playground badge when sent from the Playground), **Tokens in / out** and
**Cost**. The filter bar holds the range, team, key, requested and resolved names; the
other filters are under **More filters**. The copy button next to the date copies the
request id.

A row shows where a call went:

`requested` → `alias` → `router` → (`fallback`) → `model` → `upstream`/`upstream_model`

- `requested` is the name the client sent, kept even when it does not exist.
- `alias`, `router` and `fallback` describe how the name was resolved.
- `model` is the model that served the request; `upstream` and `upstream_model` are what
  the final attempt used.
- The rest of the row is the HTTP status and outcome (`completed`, `client_cancelled`,
  `upstream_error`, `rejected` or `unspecified`), whether the response was streamed, the number of
  upstream attempts, prompt and completion tokens, audio duration, cost, usage state and
  duration, plus the key, team and owner.

No prompt, response or header content is stored. To see the per-attempt detail of one
request, copy its `request_id` and look it up in
[Metrics, alerts and tracing](../operations/observability.md#looking-up-a-request).

## The API

`GET /api/v1/requests` (`listRequests`) returns `Page[RequestOut]`:

| Parameter | |
| --- | --- |
| `kind` | `llm` (default), `mcp` or `a2a`. MCP and A2A rows must be asked for explicitly; they have no model, tokens or cost. |
| `from`, `to` | Range; `from` is inclusive and `to` is exclusive. The default is the last 24 hours. |
| `team_id`, `key_id` | Requests of one team or key. |
| `user_id`, `service_account_id` | Requests whose key is owned by this user or service account; each requires `team_id`. |
| `requested` | Only requests that called this exact name, including one that never existed. |
| `model`, `alias`, `router` | Only requests served by or routed through this name; follows the same current-name/orphan-spelling rule as `/usage`. |
| `status`, `outcome` | HTTP status (`100`–`599`) or outcome. |
| `request_id` | One request by the gateway's id. |
| `limit`, `cursor` | Page size and the opaque cursor from the previous page's `next_cursor`. |

Rows are ordered by the usage event's timestamp (`ts`) and `request_id`, newest first,
and paginated with a keyset cursor, so a page stays stable while new requests arrive.

The permission scope is `/usage`'s: a plain member sees requests made with their own
user-owned keys, a team admin every key in the teams they administer (including by org or
installation inheritance), and an installation admin everything. An out-of-scope
`team_id` or `key_id` answers as it does on `/usage` (404 or 403).

The log exists only with a control plane: it is built from the usage events the gateways
report. Like Usage, it is best effort: an event lost by a gateway crash or a full queue is
missing here too. A standalone gateway has no request log.

## Playground

A request sent from the Admin UI Playground carries `playground_user_id`, the signed-in
user who sent it. The attribution comes from the gateway's `x-lumewarp-request-id`
response header; a response without a valid one has no Playground user. Playground calls
are not audit entries.

An administrator who uses a service account's key in the Playground is therefore visible to
everyone who can read that team's requests, team admins included.
