# Keys, limits and budgets

- **API key**: belongs to one team, one owner and exactly one zone. The zone is chosen when
  the key is created and cannot be changed by rotation. A key carries only its own
  `rpm_limit`, `tpm_limit` and `max_parallel` (NULL = no key level). The compiler resolves
  the selected zone's limits and the key's global grants into its effective policy. The
  snapshot carries only the key id and hash, team id, status, expiry and effective policy;
  owner identity stays in the control plane.

## The quota hierarchy

One resolver (`quota.py`) computes org -> team -> owner for the compiler, `/users/me` and
every write validation, within one zone at a time. Model grants are global, but a name is
usable in a zone only when its target is deployed there. Limits and budgets are stored per
zone: the minimum rate limit down the chain wins; a member's effective limits are
`exception ?? team default` for that zone, then clamped by the team, and a service account
never takes the member defaults. Remaining budget is `min(org, team, owner)` for that zone,
each from its own zone's accumulated spend.

A key is the last level: with `model_access: "inherit"` (the default) it calls whatever its
owner may, and with `"list"` it narrows to its own `allowed_models` and `allowed_groups`,
never beyond the owner's effective models (422 for a name outside them). `KeyOut` returns
`granted_models` (the key's own grants) and `owner_models` (the owner's effective models,
`null` for every name), and the key's effective models are their intersection. In the Admin
UI the key dialogs have a **Models** control (Inherit or Selected) scoped to the owner's
models, and the key tables show what each key can call.

Rate limits and budgets are edited for the selected zone, not on org or team create/update
bodies. `PUT /zones/{zone}/limits` sets an org, team, member or service-account limit and
budget in that zone; `PUT /zones/{zone}/member-defaults` sets a team's member defaults there.
Owner responses expose `limits_by_zone` (and teams expose `member_defaults_by_zone`). Model
grants remain global. Writes only check values they actually change: a changed limit may
not exceed its parent's current effective value in that zone (422), while an unchanged old
value is not rechecked, so a parent decrease always succeeds and the effective value clamps.
Team quota and model grants are `TEAM_QUOTA_SET` (org level and above); member defaults,
member exceptions, service accounts and any key's limits are team-admin-and-above. A key's owner may change their own
key's limits within their effective limits; a service-account key's limits only by team
admin and above. Every key has at most four rate-limit scopes (org, team, owner, key); the
control plane (`compiler/validate.py`) and the gateway (`snapshot.rs`) both reject more.
A key without limits emits no key scope and costs nothing on the request path.

## Key rules and lifetime

A key is created in the selected zone (`KeyCreate.zone`; omitted by the API means the
configured default zone). The zone can require an expiry, cap lifetime, set the minimum
creator role, cap each member's user-key count, and allow or forbid service-account keys.
The effective member count cap is the lower of the zone and team caps, counted within that
zone; service-account keys do not count. For a user-owned key, lifetime is capped by the
lower of the zone and team limits; for a service-account key, the zone limit applies. A key
without an expiry gets the effective cap as its expiry when one applies. When expiry is
required, a missing expiry is rejected.

Creation checks the caller's role against the zone minimum (installation admins may always
create keys; for service-account keys the caller's role is checked). Creation, rotation,
updates and resume enforce the applicable current zone rules. Tightening a rule does not
silently revoke an existing key; its `rule_violations` are returned for administrators to
review. Lowering a count cap only rejects later creates; rotation does not add a key.

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

`GET /users/me` returns per-zone effective values in `by_zone`: model names (the global
permission chain intersected with names available in that zone), effective rate limits,
key rules and count, and current UTC-month remaining budget. The org and team budget amounts
are only shown to a team admin or installation admin; a plain member sees the computed
remainder. It never returns the team's full allowlist to a member whose own policy is
narrower.

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

Usage is attributed to the key's zone, team and owner: the event's zone and `team_id` must
match the key, and the owner (member or service account) is read from the key. A member's
usage/rate-limit scope id is `member_scope_id = uuid5(team_id, user_id)`; it is derived, so
removing and re-adding a member keeps the same total and bucket within each zone. A service account uses
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

Org budgets are derived separately in each zone: the zone's org spend is the sum of its
teams' `usage_totals` (archived included). Only an org with a budget in that zone is checked
after an ingest. The first crossing inserts that zone's `org_budget_windows` row with
`ON CONFLICT DO NOTHING`, so its audit and recompile happen once even when several teams
report concurrently; a batch that cannot see a sibling's uncommitted total is caught by the
next ingest or compile. Raising or removing a budget clears that zone's current-month
window, so a later crossing audits and recompiles again.
