# Usage and audit

Usage aggregates gateway requests; [Requests](./requests.md) lists them one by one, and the
audit log records administrative changes. Usage and Requests come from the same recorded
usage events; audit answers a different question.

## Usage

Every request a gateway finishes becomes one usage event, reported to the control plane
about once a second. The Usage page and `GET /api/v1/usage` aggregate them by team, owner,
key, name, endpoint and model type. A member sees only their own keys' usage; a team admin
sees the team's; an org admin its org's teams; an installation admin everything.

![Usage by day, by model](/screenshots/usage.png)

The filter bar matches the one on Requests: the range (both days inclusive), team, model and
**Group by** on one row, and upstream model, alias, router, endpoint and model type under
**More filters**, whose button counts the ones set. The MCP and A2A tabs have the range, team
and **Group by** only.

Grouped by **API key**, each key is a pill; hover or focus it for its org, team, owner or
service account (when known), prefix and id. `GET /api/v1/usage` rows grouped by
`key` carry `key` in the same shape as the request log's `key`.

An event records the name the client called, the model that served it, the alias, router
and fallback it went through, the number of attempts, tokens, audio duration and cost.
Prompts and responses are never recorded. The same events back the
[request log](./requests.md), which lists one row per request with the full
name-resolution chain; to look at one request's per-attempt detail, use its
`x-lumewarp-request-id` in your tracing backend
([Metrics, alerts and tracing](../operations/observability.md)).

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
