# Control plane plugins (Python)

Plugins run **only** in the control plane and produce declarative output that the compiler
bakes into the snapshot; they never run in the gateway request path. They are discovered
through Python entry points, so install the package into the same environment (`uv pip
install -e ./your-plugin`) and restart `serve`:

| Group | Purpose | Status |
| --- | --- | --- |
| `lumewarp.auth` | Map an external identity (OIDC, LDAP, ...) to a user's email and display name. | Called on a first SSO login; the first plugin (by entry-point name) wins. |
| `lumewarp.authz` | Answer relation questions. | Named by `LUMEWARP_AUTHZ_BACKEND`, see [Authorization](../admin/index.md), which has its own contract (no `api_version`). |

Router strategies and the two other kinds of gateway plugin are **not** Python: a gateway
loads them as WASM components from its own directories at startup, so a Python plugin cannot
decide a route. See [gateway plugins](../plugins.md) and
[router strategies](../router-strategies.md).

Every auth plugin declares `api_version = 1`; other versions are skipped with a warning, as
are plugins that fail to import. Entry points may name an instance, a class or a zero-argument
factory (classes/factories are called once at load).

```toml
# your-plugin/pyproject.toml
[project.entry-points."lumewarp.auth"]
my-idp = "my_plugin.auth:Identity"
```

Auth plugins implement `async map_identity(claims) -> IdentityMapping | None`, with
`IdentityMapping(email, display_name="")`. Anything but a mapping, or an invalid email or name,
refuses the login. The mapping says who the user is and grants nothing; the email must still be
verified by the IdP.

## Writing an authorization backend

An authorization backend answers one question: does this user hold this relation on this
org or team? Lumewarp applies role inheritance itself, so a backend only answers direct
relations. `LUMEWARP_AUTHZ_BACKEND` names the entry point to load;
[Sign-in and authorization](../operations/identity.md#authorization-backends) lists the
backends that ship with the control plane.

A factory entry point may take one argument, `AuthzContext(settings, session_factory, metrics)`,
or none. It may also name an async `check` function or an object with an async `check` method
directly. An unknown name with no matching entry point aborts startup.

The only required method is:

```python
from lumewarp_control_plane.authz import Decision, Relation, Resource


async def check(user_id: str, relation: Relation, resource: Resource) -> Decision: ...
```

`Relation` is `admin` (on an org or a team) or `member` (on a team). `Resource.type` is `"org"`
or `"team"` and `id` is the UUID. `member` **must mean a direct team membership**: an org admin
does not inherit it, or `_is_member`, `/users/me` and the key reconciler would be wrong. A team
`admin` must imply `member` (team admins hold keys). Return `Decision.ALLOW`, `Decision.DENY` or
`Decision.ERROR`. `ERROR` means the backend is unavailable or timed out: the admin API answers
`503 authz_unavailable` and background jobs skip the round; it is never treated as a deny, and an
error while resolving an inherited capability is a 503 too, never an empty list. The gateway
never calls the backend; it only sees compiled snapshots.

Optional members are filled in from `check` when absent:

- `async batch_check(items)` returns one `Decision` per `(user_id, relation, resource)` in
  the same order; the default runs `check` with bounded concurrency.
- `async list_objects(user_id, relation, resource_type)` returns the object ids where the user
  holds the direct relation (`"org"` or `"team"`); Lumewarp intersects the result with the
  candidates. The older team-only `async list_teams(user_id, relation)` is still accepted.
  Without either, candidates are filtered with `batch_check`. A raised exception or an `ERROR`
  decision makes the admin API answer 503.
- `async changes(cursor)` returns a `ChangeBatch` of users whose relations may have changed
  since `cursor`, so the key reconciler suspends a removed member's keys within seconds.
  Without it the reconciler relies on its periodic rescan.
- `manages_grants: bool` defaults to `False`: memberships live in the external system and the
  member write endpoints answer `409 managed_externally`. The built-in backend sets it to `True`.

```toml
# pyproject.toml
[project.entry-points."lumewarp.authz"]
my-authz = "my_plugin.authz:check"
```

The backend conformance suite is importable from
`lumewarp_control_plane.authz.contract_tests`
(`src/lumewarp_control_plane/authz/contract_tests.py`). Run it against a backend before
setting `LUMEWARP_AUTHZ_BACKEND`; it covers the implied relations, org admin as a direct
relation only, inactive and unknown users, batch ordering, `list_objects` consistency and
`ERROR` handling. `tests/authz` runs it for the built-in backend, the HTTP backend and a live
OpenFGA when `LUMEWARP_TEST_OPENFGA_URL` is set (the dev instance from `scripts/dev-idp.sh up`
is `http://127.0.0.1:18081`).
