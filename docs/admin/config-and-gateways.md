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
  `INVALID_CONFIG`/`UNSUPPORTED_FEATURE` quarantines the version, `PERSIST_FAILED` retries.
  Rollback restores the catalog by id (including MCP servers and A2A agents, archiving the

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

## Gateways and their tokens

A gateway authenticates to the control plane with a gateway token (`lwg-…`) bound to one
gateway id. The first token comes from `lumewarp-control-plane bootstrap --gateway-id <id>`,
which also creates the config scope; later ones are created on the Gateways page or with
`POST /api/v1/gateways`. The secret is shown once and only its SHA-256 hash is stored.
Revoking a token closes that gateway's streams at once; the gateway keeps serving its last
config and cannot receive a new one until it has a valid token.

![Connected gateways](/screenshots/gateways.png)
