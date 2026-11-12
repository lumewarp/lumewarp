# Roles

Lumewarp has five roles. Each level inherits the administration of the level below it, and
no role comes from the identity provider: installation roles are assigned in Lumewarp, and
org and team relations come from the authorization backend
([Sign-in and authorization](../operations/identity.md)).

Five roles; each level inherits the administration of the level below it.

| Role | Scope | Can |
| --- | --- | --- |
| `proxy_admin` | `installation_admins`, one row | Everything (upstreams, routing, gateways, config, users, orgs, installation roles). Only this account or break-glass can transfer the role, or deactivate, delete, change the email of or mint a PAT for it. |
| `admin` | `installation_admins`, any number | The same installation-level management, except demoting or removing the `proxy_admin` or touching that account; assigns and removes other admins. |
| org admin | `admin` on `org:<id>` (`org_admins`) | Creates and archives the org's teams, sets their models, limits, parallelism and budget, assigns team admins, manages members, service accounts and any key in them, reads the org's usage and audit. The org policy itself is read-only. |
| team admin | `admin` on `team:<id>` (`team_members.role=admin`) | Team name, member key limits, member defaults and per-member exceptions, service accounts, any key in the team, members, team usage and audit. Cannot change the team's models, limits or budget. |
| member | `member` on `team:<id>` (`team_members.role=member`) | Reads the team's basic record and catalog names, creates and manages their own keys within their effective limits, sees their own usage. |

## How a request is authorized

Authorization has two layers. The action matrix below stays in Lumewarp; a backend only
answers direct relation questions. Inheritance is applied by `RequestAuthz`, never by a
backend, and a team's org always comes from Postgres (never from a request payload):

```
is_installation_admin        = break-glass or a row in installation_admins
is_org_admin(org)            = is_installation_admin or admin of org:<id>
team_admin_capability(team)  = is_org_admin(team.org_id) or admin of team:<id>
team_read_capability(team)   = team_admin_capability(team) or member of team:<id>
```

## Installation admins

An *installation admin* is a break-glass session, or a user with a row in `installation_admins`
(`proxy_admin` or `admin`). The role is read from Postgres once per request, so a revocation
applies on the caller's next request. It is never asked of an authorization backend, and a PAT
carries its holder's installation role. `proxy_admin` is unique: the first verified login
matching `LUMEWARP_INITIAL_ADMIN_EMAIL` creates it while no proxy_admin exists (or break-glass
assigns one), and it can be transferred afterwards. Every installation admin can assign and remove
`admin` rows; nobody can remove or demote the `proxy_admin` through those endpoints. The
`proxy_admin` account itself is protected: only that account and break-glass may deactivate,
delete, change its email or mint a PAT for it.

The endpoints are `GET /api/v1/installation-admins` (the rows with user email, display name and
active state), `PUT /api/v1/installation-admins/{user_id}` with `{"role":"admin"}` (idempotent;
`proxy_admin` is rejected as a normal assignment), `DELETE /api/v1/installation-admins/{user_id}`
(removes an `admin`; the proxy_admin must be transferred) and
`POST /api/v1/installation-admins/transfer` with `{"user_id": ...}` (the previous proxy_admin
becomes an `admin`). Assign and transfer require the target to have linked an IdP identity
(signed in once); an account that has never signed in gets 409 `the user must sign in once
before receiving an installation role`. Assign, remove and transfer are audited; the first-login
bootstrap is audited as `installation_admin.bootstrap`.

## Org admins

An *org admin* holds `admin` on one org (`org_admins` rows for the built-in backend). Its teams
are administered by inheritance: it reads every team of the org (with the whole-team policy
view), reads their members, service accounts, keys, usage and audit, changes their settings,
sets their quota and assigns their team admins. It never reaches another org, and it does not
become a team *member*, yet it may own keys in those teams (`keys.owners`): a user may own keys
in a team as a direct member, an installation admin or an admin of the team's org. No row is
written for that; key creation, resume and the key reconciler combine the direct relations.

## What each role may do

The API names each permission as an action. The [control plane API](../reference/control-plane-api.md)
states the role every operation needs.

| Action | Installation admin | Org admin | Team admin | Member |
| --- | --- | --- | --- | --- |
| `ORG_MANAGE`, `INSTALLATION_ADMIN_MANAGE`, `USER_MANAGE`, `ROUTING_MANAGE`, `ROUTING_READ`, `GATEWAY_TOKEN_MANAGE`, `GATEWAY_READ`, `CONFIG_READ`, `CONFIG_ROLLBACK` | yes | | | |
| `TEAM_CREATE`, `TEAM_DELETE`, `TEAM_QUOTA_SET`, `TEAM_ADMIN_ASSIGN` | yes | its org's teams | | |
| `TEAM_SETTINGS_UPDATE`, `TEAM_MEMBERS_MANAGE`, `TEAM_ADMIN_READ`, `SERVICE_ACCOUNT_MANAGE`, `KEY_MANAGE_ANY` | yes | inherited | yes | |
| `TEAM_READ`, `KEY_CREATE_OWN`, `KEY_MANAGE_OWN` | yes | inherited | yes | yes |
| `CATALOG_READ`, `OWN_TOKENS` | yes | yes | yes | yes |

`TEAM_QUOTA_SET` covers a team's model access, allowlist, rate limits, parallelism and budget;
`TEAM_ADMIN_ASSIGN` covers adding an admin, promoting or demoting an existing member (including
the caller themself) and removing an admin. `TEAM_CREATE` takes the `org_id` from the body and
`TEAM_DELETE` archives the team named in the URL; both are org-level, checked on the org of the
target, so an org admin may use them in its own org and a team admin cannot use them at all. A
team admin keeps `TEAM_SETTINGS_UPDATE` (the team name and the member key limits). A team update
decides from what actually changes (`proposed != base`), so resending the current policy values
is not a quota change. `ORG_MANAGE` is installation-level: create, rename, policy, archive and
org admin assignment.

`KEY_CREATE_OWN` passes for an org admin (it has the team's read capability), and creating a
user-owned key then checks key ownership: anyone else gets `422 not a member or admin of this
team`. Inherited keys follow the team's member defaults and key limits, and an inherited admin
has no member exception policy (add them as a member for one). `/users/me` lists in `teams` the
direct memberships plus every inherited team, with `inherited_from` (`installation` or `org`)
and `direct`; `admin_orgs` (id and name) lists the orgs the caller administers (every live org
for an installation admin), and break-glass has no user.

`GET /orgs/{id}/admins` lists the local org admin rows and every installation admin
(`inherited_from: installation`, `direct: false` without a local row; removed only at the
installation level). `GET /teams/{id}/members` lists direct members and, by inheritance, the
local admins of the team's org and every installation admin, in one user-id order; an
inherited-only row has role `admin`, `direct: false` and no membership to edit or remove. An
external backend cannot list who holds a relation, so its org admins are listed only as far as
local rows exist.

`TEAM_READ` is the team's basic record (`id`, `org_id`, `name`, timestamps and the member key
limits); a member's effective policy comes from `/users/me`, never from the team. `TeamOut` (with
the team policy) is only returned to a team admin or above; every other member gets
`TeamBasicOut`. `TEAM_ADMIN_READ` is the whole-team view: the member and service-account lists,
every key, the team's usage and its audit. A plain member gets `403` on those lists (a non-member
still gets `404`, and another org's team is a non-member), and `GET /keys/{id}` is admin-or-owner
only. `GET /teams/{id}/keys` for a plain member is forced to their own user-owned keys, ANDed
with whatever filters they sent, so asking for someone else returns nothing. Usage has the same
server-side owner predicate: a plain member sees rows of their own keys in teams they belong to,
including grouped totals, and cannot widen it with `key_id`/`user_id`/`service_account_id`.
Team-admin views and audit cover direct team admins and every inherited org team; a caller who
administers no team gets `403`, filtered or not. Audit also matches `org_id`, so an org admin
reads its org's org-level events (org policy, org admins, org budget) and the rows of its
archived teams; a team admin reads only rows attached to its teams.

`ROUTING_READ` is installation-level (upstreams, models, aliases, routers and plugins,
including their `GET`s). `CATALOG_READ` is the one routing read every authenticated caller
needs: `GET /catalog` returns only names, ids and kinds, which is what the allowlist pickers use.
