# Sign-in and authorization

Three separate things decide who may do what:

- **Sign-in** is OIDC. The identity provider authenticates and nothing more.
- **Installation roles** (`proxy_admin`, `admin`) are rows in Lumewarp's database.
- **Org and team relations** come from the authorization backend: built in, OpenFGA, HTTP,
  or your own plugin.

What each role may do is in [Roles](../admin/index.md).

## Credentials

Three token types, stored only as SHA-256 hashes, and two kinds of cookie session:

| Credential | Prefix | Used by |
| --- | --- | --- |
| Personal access token (PAT) | `lwp-` | A user over the admin API/UI; created under Settings in the UI or `POST /api/v1/users/me/tokens`. |
| Gateway token | `lwg-` | A gateway for the config stream and usage reporting; the first one comes from `bootstrap` (which also creates the scope), later ones from the gateways API/UI. |
| Virtual API key | `lw-` | Clients calling the gateway; owned by a team member or a service account of a team; the gateway only receives its hash in the snapshot. |
| OIDC session | cookie | A user logged in through [SSO](#sso-login-oidc). |
| Emergency session | cookie | The [break-glass](#emergency-access-break-glass) login: an installation admin that is not a user. |

The bearer scheme accepts PATs only. Nothing else authenticates to the admin API. No
credential is stored in plaintext: the OIDC nonce and PKCE verifier live only in the
encrypted login cookie, and an upstream stores only its `api_key_ref` name.

Installation admins live in Postgres (`installation_admins`). The first verified login whose
email matches `LUMEWARP_INITIAL_ADMIN_EMAIL` becomes the `proxy_admin`; break-glass can create
the first one when the setting is empty, and is the recovery path afterwards. Any installation
admin can then assign more admins. A user must have signed in through OIDC at least once before
receiving an installation role: otherwise the identity-link protection would leave a privileged
account unable to log in. Assign and transfer return 409 for an account with no linked identity;
let the person sign in once first. An installation admin can issue a PAT for an active user with
`POST /api/v1/users/{user_id}/tokens` using `{"name":"initial access"}` (optional
`expires_at`). The secret is returned only on creation; issuance is audited with the
actor and target user, never the secret. Team admins cannot use this endpoint.

## SSO login (OIDC)

The admin UI can sign in through any OIDC provider (Keycloak in dev). The control plane
runs the authorization code flow with PKCE itself (Authlib) and issues a server-side
session cookie. The IdP authenticates only: installation roles live in Postgres and team roles
never come from the IdP.

![The sign-in page](/screenshots/login.png)

| Setting | Meaning |
| --- | --- |
| `LUMEWARP_SECRET_KEY` | At least 32 bytes of random text; **required when SSO is enabled**. Derives the AES-256-GCM key for the login-state cookie. `serve` refuses to start with `LUMEWARP_OIDC_ISSUER` set and no key. Changing it invalidates pending logins only. |
| `LUMEWARP_ENCRYPTION_KEY` | At least 32 bytes of random text; **required to store an upstream API key** (`api_key` on `/upstreams`). Seals the key with AES-256-GCM; every gateway needs the same value to open it. Changing it makes stored keys unreadable until they are entered again. |
| `LUMEWARP_OIDC_ISSUER` | Issuer URL; unset disables SSO. |
| `LUMEWARP_OIDC_CLIENT_ID`, `LUMEWARP_OIDC_CLIENT_SECRET` | Client credentials (secret optional for a public PKCE client). |
| `LUMEWARP_OIDC_SCOPES` | Default `openid email profile`. |
| `LUMEWARP_INITIAL_ADMIN_EMAIL` | The first verified login with this email becomes `proxy_admin` while no proxy_admin exists. Changing the setting afterwards never moves the role. |
| `LUMEWARP_PUBLIC_URL` | External base URL; the redirect URI is `<public_url>/api/v1/session/oidc/callback`. |
| `LUMEWARP_OIDC_JIT_USERS` | Create a user with no grants on first login (default true). |
| `LUMEWARP_SESSION_TTL_SECONDS` | Absolute session lifetime (default 12 h). |
| `LUMEWARP_SESSION_COOKIE_SECURE` | `Secure` cookie flag (default true; turn off only for plain-http dev). |

With the Helm chart, pass `LUMEWARP_SECRET_KEY` through `controlPlane.extraEnv` with a
`secretKeyRef`, as in the [chart README's SSO
example](./index.md#sso-and-authorization-backend).

- Users are identified by the token's `iss` + `sub`. On a first login with a verified
  email, the identity links to an existing account with that email that
  has no identity from this issuer yet; otherwise a new user is created (JIT, only with a
  verified email). An account holding an installation role is never linked by email: a login
  with a new IdP identity for such an account is refused.
- No IdP claim grants any Lumewarp role: groups and roles claims are ignored. Installation
  roles are read from Postgres on every request; org and team relations come from the
  authorization backend, which only ever answers direct relations (`RequestAuthz` applies
  the org -> team inheritance).
- Inactive users cannot log in, and their existing sessions stop working immediately.
- The one-time login state is the encrypted cookie `lumewarp_oidc_state` (HttpOnly,
  `SameSite=Lax`, `Secure` per `LUMEWARP_SESSION_COOKIE_SECURE`, path
  `/api/v1/session/oidc`). It carries the state, the OIDC nonce and the PKCE code verifier,
  sealed with AES-256-GCM under a key derived from `LUMEWARP_SECRET_KEY`. The database row
  keeps only the sha256 of the state, return path, redirect URI and expiry, and is deleted on
  first use: a replayed callback, a tampered cookie, a cookie sealed with another key and a
  state from another login all fail as `invalid_state`.
- The cookie `lumewarp_session` is httpOnly and `SameSite=Lax`. Cookie-authenticated
  writes need the `X-Lumewarp-CSRF` header with the `csrf_token` from
  `GET /api/v1/session`. Bearer callers (PATs) never need it; if both a
  bearer and a cookie are sent, the bearer wins.
- `POST /api/v1/session/logout` ends the Lumewarp session (not the IdP session).
- IdP and authorization backend are kept in sync by the deployer: disabling a person in
  the IdP stops new logins, while revoking their team grants (OpenFGA) or removing them
  (built-in) is what suspends their keys.

## Emergency access (break-glass)

For when the IdP or the IAM is down, or before either exists. `POST /api/v1/session/break-glass`
with `{"password": ...}` (the UI's emergency login) sets the same `lumewarp_session` cookie as
OIDC, and writes need the same CSRF header:

- The session is an installation admin with no user behind it: it passes every check without
  asking the authorization backend, so it works while the backend is down. It has no PATs and
  belongs to no team.
- Break-glass can create the first proxy_admin or assign admins, but the target must have signed
  in through OIDC once first (409 otherwise); a newly created account has no linked identity for
  the role.
- It ends at its TTL, on logout, and at once when the configured credential changes.
- Failed attempts are delayed and limited per client address (429 `rate_limited`). The endpoint
  answers 404 when no credential is configured or the address is outside
  `LUMEWARP_BREAK_GLASS_ALLOWED_CIDRS`.
- Attempts are audited as `session.break_glass.login` with actor type `break_glass` (a burst of
  rate-limited ones as one summary row), and counted in `lumewarp_cp_break_glass_logins_total`.

Production uses the hash: `lumewarp-control-plane hash-password` reads the password from stdin
or a prompt and prints it for `LUMEWARP_BREAK_GLASS_PASSWORD_HASH`.

### Password, proxies and limits

Use `lumewarp-control-plane hash-password` to generate an argon2id hash, then set
`LUMEWARP_BREAK_GLASS_PASSWORD_HASH`. Invalid or non-argon2id hashes prevent startup.
Plaintext `LUMEWARP_BREAK_GLASS_PASSWORD` is accepted only with `LUMEWARP_DEV_MODE=true`.
Emergency sessions expire after 3600 seconds by default; the configurable
`LUMEWARP_BREAK_GLASS_SESSION_TTL_SECONDS` is capped at 14400 seconds. Disabling or
changing the configured credential invalidates existing emergency sessions.

`LUMEWARP_TRUSTED_PROXIES` is a comma-separated list of proxy CIDRs, empty by default.
For example, `LUMEWARP_TRUSTED_PROXIES=10.20.0.0/24,2001:db8:1234::/64` trusts only
those socket peers to supply `X-Forwarded-For`. The control plane walks the chain
from right to left and stops at the first untrusted address. Configure only actual
proxy networks, and have the edge proxy overwrite or append the real client address.
Untrusted peers and malformed forwarding chains cannot select a forwarded identity.
The CLI disables Uvicorn's own proxy-header rewriting so this policy sees the socket
peer; alternate ASGI models must also disable upstream rewriting of `scope.client`.

`LUMEWARP_BREAK_GLASS_ALLOWED_CIDRS` optionally restricts the resolved client addresses
allowed to attempt emergency login, using the same comma-separated CIDR syntax.
Other addresses receive 404. This restriction bypasses neither password verification
nor per-client limiting. Keep proxy trust separate from allowed client networks.

The per-client limiter tracks at most 4096 addresses and allows five verification
attempts per 60-second window. A successful verification clears that client's attempts.
Global pressure adds at most one second of asynchronous delay, never a global deny:
one attacking identity cannot lock out another identity's correct password.
Every verified success/failure is audited; rate-limited attempts increment metrics and
produce at most one summary audit row per 60 seconds per CP process.

## Authorization backends

`LUMEWARP_AUTHZ_BACKEND` selects the backend: `builtin` (the default), `openfga`, `http`, or
any other value naming a `lumewarp.authz` entry point. Every backend, the three built-ins
included, loads through that one entry point (they are registered in this project's
`pyproject.toml`), so there is no special case in `load_authorizer`. Every backend answers
direct relations only; installation roles are Lumewarp's own Postgres data and are never sent
to a backend. `builtin` answers `admin` on orgs from `org_admins` and `admin`/`member` on teams
from `team_members`.

Two external backends ship with the control plane:

- `openfga`: `LUMEWARP_OPENFGA_URL`, `LUMEWARP_OPENFGA_STORE_ID`,
  `LUMEWARP_OPENFGA_MODEL_ID`, optional `LUMEWARP_OPENFGA_API_TOKEN`. Users are
  `user:<lumewarp user uuid>`; the standalone model is `deploy/openfga/model.fga` (`org` with
  `admin`, `team` with `admin`/`member`, `admin` implies `member`, no org -> team relation).
  Implements `batch_check`, `list_objects` (ListObjects) and `changes` (ReadChanges), all
  through the same name mapping:
  - `LUMEWARP_OPENFGA_OBJECT_TYPES='{"team": "llm_team", "org": "llm_org"}'` renames types;
  - `LUMEWARP_OPENFGA_RELATIONS='{"team.admin": "owner"}'` renames relations.

  Defaults are today's names. `deploy/openfga/model-renamed.fga` is the same model under
  `llm_org`/`llm_team` with `owner` as the team admin relation. `changes()` maps every changed
  tuple back to Lumewarp users; a change it cannot map to users (a userset or wildcard subject)
  asks the reconciler for a full rescan instead of being dropped. In a store shared with other
  applications, a change to any other object type (or to a relation Lumewarp does not map) makes
  the reconciler check that user, or rescan everything when no user can be named: expect extra
  checks in a busy store. `scripts/dev-idp.sh up`
  starts a local OpenFGA, writes the model and prints these variables.
- `http`: `LUMEWARP_AUTHZ_HTTP_URL` (+ optional `LUMEWARP_AUTHZ_HTTP_TOKEN`).
  `check` becomes `POST {"user_id", "relation", "resource": {"type", "id"}}` answered by
  `{"decision": "allow" | "deny"}`, with `type` `"org"` or `"team"`; anything else (non-2xx,
  timeout, bad body) is `ERROR`.

Every backend call is bounded by `LUMEWARP_AUTHZ_TIMEOUT_SECONDS` (default 2 s); a
timeout is `ERROR`.

OpenFGA Check and BatchCheck always request `HIGHER_CONSISTENCY`, including
checks made by the key reconciler, so suspension decisions do not use cached
permission results.

The contract a backend implements, and how to write one, is in
[Control plane plugins](../extending/python-plugins.md).

## Key reconciler

A user-owned key stays active only while its owner is a `member` of the key's team. The
`key_reconciler` background loop enforces this for external backends (the built-in
backend already suspends keys in the same transaction that removes the member):

- With `changes()` (OpenFGA) it polls every `LUMEWARP_KEY_RECONCILE_POLL_SECONDS`
  (default 2 s) and rechecks only the users that changed, so a removed member's keys are
  rejected by the gateway within about 5 s.
- Every `LUMEWARP_KEY_RECONCILE_FULL_SECONDS` (default 300 s), and whenever the change
  cursor is reset, it rechecks every active user-owned key (in slices).
- Ownership is checked as in `keys.owners` (direct member, installation admin, admin of the
  team's org). When every source answers an explicit `DENY`, the owner's keys in that team
  are suspended with
  `suspended_reason=owner_removed`, audited with the system actor `key_reconciler`, and
  publishes a new snapshot. `ERROR` or a timeout never suspends anything.
- Restoring membership or an admin role does not resume keys automatically; the owner or a team admin
  resumes them. Service-account keys are never touched: rotate them when people leave.
- `LUMEWARP_KEY_RECONCILE_ENABLED=false` turns the loop off.
