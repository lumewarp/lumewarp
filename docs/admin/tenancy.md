# Orgs, teams and members

There is one installation with multiple config zones. Orgs, teams, memberships, users and
roles are global; each zone has its own upstreams, deployments, gateway tokens, keys, limits,
budgets and usage. Below the global org → team hierarchy are key owners (members and service
accounts) and keys. This page describes tenancy; zone-specific limits and budgets are in
[Keys, limits and budgets](./keys-and-limits.md) and [Zones](./zones.md).

![A team's members, with an inherited installation admin](/screenshots/team.png)

- **Org**: the root of the global model-grant hierarchy. Per-zone rate limits and budgets
  are configured separately through the zone limits API, not on org create/update bodies.
  Only an installation admin may create one (`POST /orgs`), rename or set its grant policy (`PATCH /orgs/{id}`),
  archive it (`DELETE /orgs/{id}`) or assign/remove its admins
  (`PUT|DELETE /orgs/{id}/admins/{user_id}`); an org admin reads its own orgs
  (`GET /orgs`, `GET /orgs/{id}`) and their per-team spend (`GET /orgs/{id}/usage`, summed
  from `usage_events` at query time, optionally filtered by zone). Admin assignment needs the built-in backend
  (`manages_grants`; an external backend answers 409) and a target that has signed in
  once. An **org admin** holds `admin` on `org:<id>`; `RequestAuthz` inherits that for
  every team of the org. `POST /teams` takes an explicit `org_id`: there is no default
  org. Archiving an org is irreversible and only allowed once it has no live team; after
  that no team can be created in it.
- **Team**: a global name, its `org_id`, a model-grant policy, and self-service limits
  `max_keys_per_member` (non-revoked user keys per member; revoking frees a slot) and
  `max_member_key_lifetime_seconds`. Rate limits, budgets and member-default limits are
  configured per zone. `DELETE /teams/{id}` archives: in one transaction it locks the
  hierarchy, sets `deleted_at`, revokes every key of
  the team (including the rotation-grace hash), deactivates its service accounts and
  frees the name, which is unique only among live teams. Archived teams stay in the
  database for usage, the request log and audit (`GET /usage?team_id=`,
  `GET /requests?team_id=`, `GET /audit?team_id=`, the org usage summary) but every
  write path and every team-scoped read answers 404.
- **Model grants are global; availability is per zone.** The model access picker of a team
  lists only its org's `effective_models`; the picker of a member or service account lists
  only the team's `effective_models`. A granted model may still be unavailable in a zone
  without a deployment. Names granted earlier that fell outside the parent policy stay
  visible under "Not offered" so they can be unticked. [Model groups](./catalog.md#model-groups) are
  granted alongside names (`allowed_groups`) and show their members as "via `group`"; a
  group with any member outside the parent policy is disabled, because it could not be granted
  whole.
- **Team member**: a user in a team with role `admin` or `member`. The member's own
  exception policy and grants live in `member_policies`, keyed by `(team_id, user_id)` and
  deliberately independent of `team_members`: an external authorization backend has no
  membership rows, yet per-member grants and per-zone limits still apply. Direct membership determines team
  visibility (`team_members` for the built-in backend) and the `member` relation. A key owner
  may be a direct team member, an installation admin or an admin of the team's org; team
  administration and key ownership can be inherited. An installation admin sees every team.
- **Service account**: a non-human key owner in one team, with its own narrowing policy,
  managed by the team's admins. It takes no member defaults and is unaffected by people
  joining or leaving. `PATCH /service-accounts/{id}` with `{"is_active": false}`
  deactivates it: the compiler leaves its keys out of the snapshot (and includes the
  non-revoked ones again after reactivation). Archiving a team deactivates all of its
  accounts.

## Removing people

Removing a member suspends that member's user-owned keys in the team
(`suspended_reason=owner_removed`) unless they still administer it by inheritance (an
installation admin or an admin of the team's org); removing an org admin or an installation
admin does the same in every team where nothing else allows them. Deactivating a user suspends their user-owned keys
(`owner_deactivated`). Revoked keys are kept for usage history; a key or a service
account no longer prevents archiving a team. Team membership and roles apply in every zone,
but each key remains bound to its own zone.

An active owner may resume a key they suspended themselves (`owner`) and, once they
may own keys in the team again, one suspended by their removal (`owner_removed`). An `admin`
suspension needs a team admin; `owner_deactivated` needs an administrator after the
account is reactivated. Nothing can be resumed while the key's owner is inactive.

## With an external authorization backend

With an external authorization backend, `GET /api/v1/teams/{id}/members` still
lists local `team_members` rows, not the external directory. An externally
managed team with no local rows returns `{"items": [], "next_cursor": null}`;
membership writes remain disabled. Session discovery and `/users/me` resolve
`is_admin` through the configured backend. On backend ERROR both report `is_admin: false` with
`authz_unavailable: true` for display, and privileged API operations fail closed with HTTP 503.
