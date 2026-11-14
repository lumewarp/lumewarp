# Keys, limits and budgets

- **API key**: belongs to one team and is owned by a member (`owner_kind=user`) or a service
  account (`owner_kind=service_account`). A key carries only its own `rpm_limit`,
  `tpm_limit` and `max_parallel` (NULL = no key level). The compiler resolves the whole
  chain into each key's effective policy. The snapshot carries only the key id and hash,
  team id, status, expiry and effective policy; owner identity stays in the control plane.

## The quota hierarchy

One resolver (`quota.py`) computes org -> team -> owner for the
compiler, `/users/me` and every write validation. Models are the intersection down the
chain (`INHERIT`/`ALL` keep the parent, `LIST` intersects). Limits are the minimum down the
chain; a member's effective limits are `exception ?? team member_default`, then clamped by
the team, and a service account never takes the member defaults. Remaining budget is
`min(org, team, owner)`, each from its own accumulated spend: the org has no
`usage_totals` row, its spend is the sum of every one of its teams (archived included).

Writes only check values they actually change: a changed value may not exceed the parent's
current effective value (422), while an unchanged old value is not rechecked, so a parent
decrease always succeeds and the effective value clamps. Team totals and models are
`TEAM_QUOTA_SET` (org level and above); member defaults, member exceptions, service
accounts and any key's limits are team-admin-and-above. A key's owner may change their own
key's limits within their effective limits; a service-account key's limits only by team
admin and above. Every key has at most four rate-limit scopes (org, team, owner, key); the
control plane (`compiler/validate.py`) and the gateway (`snapshot.rs`) both reject more.
A key without limits emits no key scope and costs nothing on the request path.

## Key lifetime

A member key can be created with an optional `expires_at`. When the team sets
`max_member_key_lifetime_seconds`, a member key created without one gets an expiry at that
cap, and a supplied expiry beyond it is a 422. Rotation and `PATCH /keys/{id}` enforce the
same cap (a never-expiring key gets a deadline at rotation; an existing later expiry must
be shortened first). Team admins and installation admins are subject to these checks;
service-account keys are not member keys and are exempt. Lowering the caps never revokes
existing keys: lowering the lifetime cap rejects later creates, rotations or expiry
extensions that would exceed it, while lowering the count cap only rejects later creates,
because rotation does not add a key.

## Rotation

`POST /keys/{id}/rotate` accepts an optional `{"grace_seconds": N}` (0 by default, at most
86400). With a grace, the replaced hash stays valid until its own expiry (bounded by the
key's `expires_at`) and authenticates as the same `key_id`, so usage stays on one key. The
gateway stores it next to the current hash and enforces that expiry from its persisted
snapshot even while the control plane is down. The control plane drops an expired previous
hash on its next compile, which sends the delta that removes it. Only one previous hash is
kept: rotating again inside the grace ends the older grace immediately. Revoking clears
both hashes. The response exposes `previous_key_expires_at` but never the old secret.

## What a member sees

`GET /users/me` returns, for every team the caller belongs to, the effective model names
(the whole chain, org included; `all_models` when that intersection is universal), the
effective rate limits (org, team and member defaults included), the team's member-key limits
with the caller's current non-revoked key count (`key_limits`), and the current UTC-month
org/team/member budgets with the remaining amount computed server-side. The org and team
budget numbers themselves are only shown to a team admin or installation admin; a plain
member sees the computed remainder. It never returns the team's full allowlist to a member
whose own policy is narrower.

## Limits on key writes

`POST /teams/{id}/keys` (create), `POST /keys/{id}/rotate` and `PATCH /keys/{id}` are rate
limited per principal and operation: 30 attempts per minute each, independent of the target
team or key. The check runs after authentication, before target lookup and authorization.
Installation admins and break-glass sessions are exempt; a member who is only a team admin
is not. A refusal is a 429 `rate_limited` with `Retry-After`, counted in
`lumewarp_cp_key_writes_total{result="rate_limited"}` and deliberately not audited: only
successful writes are audited, so a client cannot flood the audit table. A limits-only
`PATCH` never touches the key's lifecycle or suspension reason.

## Suspended keys

An active owner may resume a key they suspended themselves (`owner`) and, once they
may own keys in the team again, one suspended by their removal (`owner_removed`). An `admin`
suspension needs a team admin; `owner_deactivated` needs an administrator after the
account is reactivated. Nothing can be resumed while the key's owner is inactive.

## Attribution and rate-limit scopes

Usage is attributed to the key's team and owner: the event's `team_id` must match the
key, and the owner (member or service account) is read from the key. A member's
usage/rate-limit scope id is `member_scope_id = uuid5(team_id, user_id)`; it is derived, so
removing and re-adding a member keeps the same total and bucket. A service account uses
its own id. Rate-limit scopes are `org:<org_id>`, `team:<team_id>`,
`member:<member_scope_id>`, `sa:<service_account_id>` and `key:<key_id>`; a level that sets
no limit emits no scope. Rate limits are counted per gateway unless the gateways
share a Redis and namespace (`--shared-limits-redis-url`): then the gateways share one
window and may over-admit by about the number of active gateways, plus up to five
sync intervals of traffic when a gateway first joins the scope; without Redis, N gateways
allow up to N times the configured limit. `requests_per_minute` and
`tokens_per_minute` use a sliding window on wall-clock minutes: the current and the
previous minute count, with the previous one fading out as the minute elapses, so a
burst is not followed by a second full burst at the next minute boundary.
`max_parallel_requests` counts requests in flight and releases the slot when the response
body ends, errors or is dropped. `tokens_per_minute` is charged after usage is known, so it
blocks later requests instead of rejecting the one that produced the tokens. A
rejection is 429 `rate_limit_exceeded` with `Retry-After`.

## Org budgets

Org budgets are derived: the org's monthly spend is the sum of its teams' `usage_totals`
(archived included), and only orgs with a budget are checked after an ingest. The first
crossing inserts `org_budget_windows` with `ON CONFLICT DO NOTHING`, so the audit row and
the recompile happen exactly once even when several teams of one org report concurrently;
a batch that cannot see a sibling's uncommitted total is caught by the next ingest or
compile. Raising a budget (or removing it) deletes the month's window row, so a later
crossing audits and recompiles again instead of staying silent.
