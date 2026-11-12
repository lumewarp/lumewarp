# Usage and audit

## Usage

Every request a gateway finishes becomes one usage event, reported to the control plane
about once a second. The Usage page and `GET /api/v1/usage` aggregate them by team, owner,
key, name, endpoint and model type. A member sees only their own keys' usage; a team admin
sees the team's; an org admin its org's teams; an installation admin everything.

![Usage by day, by model](/screenshots/usage.png)

An event records the name the client called, the model that served it, the alias, router
and fallback it went through, the number of attempts, tokens, audio duration and cost.
Prompts and responses are never recorded. To look at one request in detail, use its
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

Every mutation writes an `audit_log` row in the same transaction. Reads are scoped: an
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
