# Requests

**Requests** is the request log: one row per gateway request, newest first. Each event is
attributed to the key's zone. Usage aggregates the same events; the audit log records
administrative changes only. The header zone selects the view; choose **All zones** to see
every zone.

![The Requests page](/screenshots/requests.png)

The page shows one row per request: **Date**, **Status** (the HTTP status, green for 2xx,
amber for 4xx and red for 5xx, with the outcome when the request did not complete),
**Requested** (the name the client called), **Resolved** (the model that served it; only an
installation admin sees it), **Key** (a pill with the key name; hover or focus it for the
org, team, owner (or service account), zone, `lw-` prefix and id),
**Tokens** (total, with input + output in brackets), **Cost**, and the gateway's **error code** when the request failed. The filter bar holds the
range (**From** and **To**, date and time), zone, team, key and requested name; the other filters, including the resolved names, are
under **More filters** and only an installation admin sees the routing ones. The copy button
next to the date copies the request id.

**From** and **To** are date and time pickers in the browser's time zone. **From** is inclusive; **To** includes the chosen minute, so the page sends the
next minute as the API's exclusive `to`. Empty **From** starts 24 hours ago and empty **To** ends
now. A link with a bare day (`from=2026-11-20`, as older bookmarks have) opens as that local
day: **From** at 00:00 and **To** at 23:59.

## Request details

Click a row to open its details beside the log.

![A request's details](/screenshots/request-detail.png)

**Route** is a timeline of where the call went:

`requested` → `alias` → `router` → (`fallback`) → `model` → `upstream`/`upstream_model` → response

- **Received** is the name the client sent, kept even when it does not exist, with the
  endpoint, the key pill and whether the response was streamed.
- **Alias**, **Router** and **Fallback** appear only when the name was resolved through them.
- **Model** is the model that served the request; **Upstream** is the upstream and its model
  name used by the final attempt, with the number of attempts when there were retries.
- **Response** is the HTTP status, the outcome (`completed`, `client_cancelled`,
  `upstream_error`, `rejected` or `unspecified`), the duration, the **error code** of a failed
  request (a gateway code, `upstream_<status>` for an error the upstream answered, or
  `client_cancelled`) and, for a successful stream, the time to the first token (`ttft_ms`).

Only an installation admin sees the routing part of the timeline (alias, router, fallback,
model, upstream and attempts). Every other caller sees the received name, the response,
usage and sender; the routing fields are null in the API and hidden in the page. This also
covers a team or org admin: they see the team's requests, but by the name clients called,
not what a router or alias resolved to.

A step the request never reached is left out, so a request rejected before routing shows
only **Received** and **Response**. **Usage** shows the tokens, audio duration, cost and whether the upstream
reported usage; **Sender** shows the key pill, team, owner, who sent it from the Playground,
and the request id with a copy button.

No prompt, response or header content is stored. To see the per-attempt detail of one
request, copy its `request_id` and look it up in
[Metrics, alerts and tracing](../operations/observability.md#looking-up-a-request).

## The API

`GET /api/v1/requests` (`listRequests`) returns `Page[RequestOut]`:

| Parameter | |
| --- | --- |
| `kind` | `llm` (default), `mcp` or `a2a`. MCP and A2A rows must be asked for explicitly; they have no model, tokens or cost. |
| `zone` | One zone; omitted by the API means all zones. The Admin UI defaults to the header zone. |
| `from`, `to` | Range; `from` is inclusive and `to` is exclusive. The default is the last 24 hours. |
| `team_id`, `key_id` | Requests of one team or key. |
| `user_id`, `service_account_id` | Requests whose key is owned by this user or service account; each requires `team_id`. |
| `requested` | Only requests that called this exact name, including one that never existed. |
| `model`, `alias`, `router` | Only requests served by or routed through this name; installation admin only, otherwise `403 routing_detail_forbidden`. Follows the same current-name/orphan-spelling rule as `/usage`. |
| `status`, `outcome` | HTTP status (`100`–`599`) or outcome. |
| `error_code` | Only requests that ended with this gateway error code (the usage event's `error_code`). |
| `request_id` | One request by the gateway's id. |
| `limit`, `cursor` | Page size and the opaque cursor from the previous page's `next_cursor`. |

Rows are ordered by the usage event's timestamp (`ts`) and `request_id`, newest first,
and paginated with a keyset cursor, so a page stays stable while new requests arrive.

Each row carries `error_code` and `ttft_ms`, both null when unknown: a request without an
error has no code, a request that did not stream has no first-token time, and rows stored
before these fields existed have neither. Each row carries its `zone`, `key_id` and `key`, the shared key reference (`id`, `name`,
`display_prefix`, `team_id`, `team_name`, `org_id`, `org_name`, `owner_kind`, `user_id`,
`service_account_id`, `service_account_name`, `lifecycle`). `key` is null when the key row
no longer exists; `key_id` remains and the UI shows **Deleted key**.

The routing chain is installation-admin-only. For every other caller the row's `model`,
`model_id`, `alias`, `alias_id`, `router`, `router_id`, `upstream`, `upstream_model`,
`fallback` and `attempts` are null; `requested` stays the name the client called. The
`model`, `alias` and `router` filters answer `403` with the error code
`routing_detail_forbidden` for them, so they cannot probe how a name resolves.

A renamed model, alias or router appears under the called spelling it had at the time; the
request log never rewrites historical rows to the current catalog name.

The permission scope is `/usage`'s: a plain member sees requests made with their own
user-owned keys, a team admin every key in the teams they administer (including by org or
installation inheritance), and an installation admin everything. Unlike `/usage` totals,
requests stay per-request: a plain member does not see a colleague's requests even without
an owner or key filter. An out-of-scope `team_id` or `key_id` answers as it does on `/usage`
(404 or 403).

The log exists only with a control plane: it is built from the usage events the gateways
report. Like Usage, it is best effort: an event lost by a gateway crash or a full queue is
missing here too. A standalone gateway has no request log.

## Playground

A request sent from the Admin UI Playground carries `playground_user_id`, the signed-in
user who sent it, shown under **Sender** in the request's details. The attribution comes from the gateway's `x-lumewarp-request-id`
response header; a response without a valid one has no Playground user. Playground calls
are not audit entries.

An administrator who uses a service account's key in the Playground is therefore visible to
everyone who can read that team's requests, team admins included.
