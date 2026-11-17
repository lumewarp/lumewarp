# Usage and audit

Usage aggregates gateway requests; [Requests](./requests.md) lists them one by one, and the
audit log records administrative changes. Usage and Requests come from the same recorded
usage events; audit answers a different question.

## Usage

Every request a gateway finishes becomes one usage event, reported to the control plane
about once a second. The Usage page and `GET /api/v1/usage` aggregate them by team, owner,
key, called name, endpoint and model type.

Only an installation admin sees where a request was served: the `model`, `upstream_model`,
`alias` and `router` dimensions, their ids and `_missing` filters, and the `served_by`,
`via_aliases`, `via_routers` and `upstream_model(s)` provenance. Any other caller gets
`403` with the error code `routing_detail_forbidden` for those dimensions or filters, and
the provenance fields are null or empty. Everyone else groups by the name the client
called (`requested`) together with `day`, `team`, `owner`, `key`, `endpoint`, `model type`,
`kind`, `target`, `method` and `tool`.

Visibility follows the caller's role:

- A plain member sees the **whole team's totals** when the query neither groups nor filters
  by owner or key: the team's requests by called name, day, endpoint and so on are summed
  over every key in the team, service accounts included.
- Grouping by `owner` or `key`, or filtering by `user_id`, `service_account_id` or `key_id`,
  falls back to the narrower rule: in a team the member does not administer they see only
  their own user-owned keys. Other members' keys and owners are never split out for a
  plain member, whatever they ask for.
- A team admin (including an org admin by inheritance) and an installation admin keep the
  whole-team view, including owner and key grouping and filtering.
- An installation admin sees every team as well.

A renamed model, alias or router appears as separate called names before and after the
rename: usage stores the name the client sent, not the catalog's current name. The
`requested` filter therefore selects that exact spelling.

![Usage by day, by model](/screenshots/usage.png)

The filter bar matches the one on Requests: the range (both days inclusive), team, model and
**Group by** on one row, and upstream model, alias, router, endpoint and model type under
**More filters**, whose button counts the ones set. The MCP and A2A tabs have the range, team
and **Group by** only. The routing-specific filters and group-by choices are only available
to an installation admin.

Grouped by **API key**, each key is a pill; hover or focus it for its org, team, owner or
service account (when known), prefix and id. `GET /api/v1/usage` rows grouped by
`key` carry `key` in the same shape as the request log's `key`.

An event records the name the client called, the model that served it, the alias, router
and fallback it went through, the number of attempts, tokens, audio duration and cost. Only
an installation admin sees the served model and routing chain; every other caller sees the
called name. Prompts and responses are never recorded. The same events back the
[request log](./requests.md), which lists one row per request with the full
name-resolution chain; to look at one request's per-attempt detail, use its
`x-lumewarp-request-id` in your tracing backend
([Metrics, alerts and tracing](../operations/observability.md)).

## Leaderboard

`GET /api/v1/usage/leaderboard` (`getUsageLeaderboard`) ranks the caller's readable teams by
tokens and spend. Installation admins rank every team; everyone else only the teams they
can read. `by` selects the board:

| `by` | Ranks | Extra row fields |
| --- | --- | --- |
| `requested` | The names clients called, summed across the readable teams | `requested` |
| `key` | API keys, grouped within their team | `team_id`, `key` (when readable) |
| `owner` | Users and service accounts, grouped within their team | `team_id`, `owner_kind`, `owner_id`, `is_me` |

Rows are ordered by `total_tokens` descending, then `cost_nano_usd`, and numbered from
`rank=1`. The other query parameters are the same range (`from`, `to`, defaulting to the
current UTC month), `team_id`, `kind` (`llm` by default) and `limit` (default 10, maximum
50). Each row also carries `requests`, `prompt_tokens`, `completion_tokens`, `total_tokens`,
`cost_nano_usd` and `unknown_cost_count`.

An identity is named only when the caller may read it. For `by=key`, `key` is a
[key reference](./requests.md#the-api) for an installation admin, the key's team admin, or
the key's user owner; otherwise it is null and `label` is `Key #<rank>`. For `by=owner`,
`owner_kind` and `owner_id` are filled for an installation admin, that team's admin, or the
row's own owner; otherwise both are null and `label` is `Member #<rank>`. In both cases the
unreadable key's or owner's id stays hidden, and `label` names the readable row. `is_me` is
true only for the caller's own owner row, which is always named.


  ones absent from the state so their definitions and grants survive) and republishes an
  older version's **routing** only; keys
  and policies always come from current source state. Full rules: [config protocol](../config-protocol.md).
`UsageService.Report` validates per event (an invalid event is skipped and logged, the
  rest of the batch is kept; `REJECTED` events may omit model/model, and an MCP or A2A event
  carries no model, tokens or cost) and is idempotent by `request_id`; each new LLM event
  adds cost once to the team total and once to the key owner's (member or service account)
  total for the UTC calendar month in the same transaction, then budget evaluation flips
  exhausted scopes to `BUDGET_EXCEEDED` and publishes a new version (eventual; in-flight
  streams are not cut). MCP and A2A events (`kind=mcp|a2a`, with `target`, `method` and
  `tool`) are recorded and never aggregated: no budget, no limit, no cost. Budget publishing targets the protocol's 5 s
  eventual-enforcement window; it is not a configurable per-flip timeout. The unused
  `LUMEWARP_BUDGET_FLIP_DEADLINE_SECONDS` setting has been removed.

Usage is stored in Postgres; ClickHouse is later. While the control plane is down the
  gateway keeps serving from its snapshot, but usage batches queue in memory and are lost

## Audit

The audit log records administrative changes only. A model request, including one sent
from the Playground, is not an audit entry; it appears in [Requests](./requests.md)
instead. The page filters by range (empty means no bound; the end day is inclusive), org,
team and action, in the same filter bar as Usage and Requests. Every management mutation writes an `audit_log` row in the same transaction. Reads are scoped: an
installation admin sees everything (including rows without a team and rows of archived teams), a
team admin sees the rows of the teams they administer (filtered or not), an org admin additionally
sees its org-level events (`org_id` set, `team_id` NULL: org policy, org admins, org budget) and
the rows of its archived teams, and a plain member has no audit view (`403`). `details` is an
allowlist per action (`DETAIL_ALLOWLIST` in `audit.py`): only fields classified for that
action are stored, and anything else is dropped rather than redacted. Nested objects such
as `changes`, `settings` and `traffic_split` have their own field lists, and free-form
strategy params are never recorded. The failure column (`config_outbox.last_error`)
keeps only the exception class name. A test scans every
`audit.record(action=...)` call site, so a new action without a classification fails the
suite, and `tests/test_schema_secrets.py` fails when a new Text/String/JSONB column is not
classified as non-secret.

![The audit log](/screenshots/audit.png)

A key resource is shown as the same key pill; other resource types keep their type and a
short id. `GET /api/v1/audit` entries with `resource_type: "key"` carry `key`.
