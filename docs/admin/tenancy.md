# Orgs, teams and members

There is exactly one installation. Below it: org → team → key owners (members and service
accounts) → keys. This page describes each level; limits and budgets along the chain are in
[Keys, limits and budgets](./keys-and-limits.md).

- **Org**: the root of the quota hierarchy: `model_access` and `allowed_models`,
  `rpm_limit`, `tpm_limit`, `max_parallel` and `budget_nano_usd`. Only an installation
  admin may create one (`POST /orgs`), rename or set its policy (`PATCH /orgs/{id}`),
  archive it (`DELETE /orgs/{id}`) or assign/remove its admins
  (`PUT|DELETE /orgs/{id}/admins/{user_id}`); an org admin reads its own orgs
  (`GET /orgs`, `GET /orgs/{id}`) and their per-team spend (`GET /orgs/{id}/usage`, summed
  from `usage_events` at query time). Admin assignment needs the built-in backend
  (`manages_grants`; an external backend answers 409) and a target that has signed in
  once. An **org admin** holds `admin` on `org:<id>`; `RequestAuthz` inherits that for
  every team of the org. `POST /teams` takes an explicit `org_id`: there is no default
  org. Archiving an org is irreversible and only allowed once it has no live team; after
  that no team can be created in it.
- **Team**: a name, its `org_id`, a policy (`model_access` and `allowed_models`,
  `rpm_limit`, `tpm_limit`, `max_parallel`, `budget_nano_usd`), the member defaults
  `member_rpm_default`, `member_tpm_default` and `member_max_parallel_default`, and the
  self-service limits `max_keys_per_member` (non-revoked user keys per member; revoking
  frees a slot) and `max_member_key_lifetime_seconds`. `DELETE /teams/{id}` archives:
  in one transaction it locks the config scope, sets `deleted_at`, revokes every key of
  the team (including the rotation-grace hash), deactivates its service accounts and
  frees the name, which is unique only among live teams. Archived teams stay in the
  database for usage and audit (`GET /usage?team_id=`, `GET /audit?team_id=`, the org
  usage summary) but every write path and every team-scoped read answers 404.
- **Team member**: a user in a team with role `admin` or `member`. The member's own
  exception policy and grants live in `member_policies`, keyed by `(team_id, user_id)` and
  deliberately independent of `team_members`: an external authorization backend has no
  membership rows, yet per-member quotas still apply. Direct membership determines team
  visibility (`team_members` for the built-in backend) and is the only way to own a key;
  team administration is also inherited from an org admin; an installation admin sees
  every team.
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
account no longer prevents archiving a team.

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
