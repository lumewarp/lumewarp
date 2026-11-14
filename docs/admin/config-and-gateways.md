# Config versions and gateways

Every change that affects what gateways enforce is compiled into a new, immutable config
version and pushed to the gateways. The Config page lists the versions and can roll the
routing back; the Gateways page shows which version each gateway runs.

## Publishing

- Every source mutation (API write, budget flip, rollover) commits with an audit row and an
  "recompile" outbox row in one transaction. One compiler loop per scope, serialized with a
  Postgres advisory lock, expands inheritance and writes an immutable, complete
  `config_versions` snapshot with digest and source revision. A failed compile leaves the
  previous version current. Serialization conflicts retry with bounded jitter. Unchanged
  compiled content reuses the latest version, including after a control-plane restart;
  publication metadata is excluded from the content comparison.
- Gateways get a full snapshot, then deltas; each update is applied, validated, persisted
  and swapped by the gateway before it ACKs. NACK `BASE_MISMATCH` triggers a full,
  `INVALID_CONFIG` quarantines the version for the whole scope, `UNSUPPORTED_FEATURE` makes
  only that gateway skip it, and `PERSIST_FAILED` retries.

## Rollback

Rollback restores models, aliases, routers, MCP servers and A2A agents by id from the
version's `catalog_state`, recreating deleted items under their old id, and re-publishes that
version's routing as a new version. An MCP server or A2A agent absent from the state is
archived rather than deleted, so its definitions and grants survive; reviewed definition
states are not captured, so a rollback never re-decides a definition. Grants are not routing
config: grants on a deleted catalog item do not come back. Keys, teams and policies come
from current state, so a revoked key stays revoked. It goes through the same plugin checks
as a router write.

![Config versions](/screenshots/config-versions.png)

## Gateways and tokens

A gateway is one process. It generates its id at startup (`<hostname>-<8 hex>`), sends it as
`Hello.gateway_id`, and keeps it until it exits; a restart is a new gateway. The Gateways page
has one row per process, connected first and then by id; a disconnected row stays for 24 hours
and is then deleted. A reconnect of the same process replaces its previous stream.

A gateway token (`lwg-…`) authenticates the config scope, not a gateway id: any number of
gateways may use one token. It has an optional label and is created by
`lumewarp-control-plane bootstrap [--name <label>] [--scope <name>]` (which also creates the
config scope), by **New gateway token** on the Gateways page, or by
`POST /api/v1/gateway-tokens`. The secret is shown once and only its SHA-256 hash is stored.
Revoking a token closes every config and usage stream that authenticated with it at once;
each affected gateway keeps serving its last good config and cannot receive a new one until
it reconnects with a valid token. The revoke response reports `connected_gateways: 0` because
those streams are already closed; the Tokens tab counted them before revocation and its
confirmation says how many will be disconnected.

### The Gateways tab

A warning above the table appears only when there is something to fix:

- connected gateways run different build versions, each named with its version;
- connected gateways disagree on shared limits, each named with its namespace or "per gateway";
- a connected gateway has an outstanding NACK, with its code and reason;
- the scope has a quarantined config version. The warning and the `quarantined vN` badge
  appear only while that version is still the newest; a newer version supersedes them.

Only an `INVALID_CONFIG` NACK quarantines a version; `UNSUPPORTED_FEATURE` stays on the
NACKing gateway's row and does not withhold the version from the others.

| Column | What it shows |
| --- | --- |
| Gateway | `Hello.gateway_id`, plain text. |
| State | Connected or Disconnected; disconnected rows are dimmed. |
| Version | The build version the gateway reported in its latest `Hello`. |
| Config | The ACKed version plus at most one badge, in priority order: quarantined > NACK > pending > behind. The NACK badge names the version; the warning above gives its code and reason. A fresh gateway with no verified snapshot reads "—". |
| Rate limits | "Per gateway", or "Shared (namespace)" when it shares counters through Redis ("Shared" alone when it reports no namespace). One gateway's setting only; connected gateways may disagree during a rollout. |
| Token | The label of the token its latest `Hello` authenticated with, or the token prefix when it has no label. |
| Last seen | Its latest status write. |
| Peer | The network peer of its config stream. |

On narrow screens the less important columns are dropped. The empty state says that no
gateways have connected yet and that a token connects one.

![Gateways](/screenshots/gateways.png)

### The Tokens tab

Columns: Label, Prefix, Created, Last used, Gateways, Status and Revoke. **Gateways** is how
many gateways are connected with that token right now. Revoked tokens stay listed with their
revocation time. **New gateway token** asks only for a label and shows the secret once.
**Revoke** asks for confirmation, naming how many connected gateways it disconnects and that
they keep serving their last good config. A revoked token's gateways cannot receive a new
config until they reconnect with a valid token.
