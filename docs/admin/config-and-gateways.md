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
which also creates the config scope; later ones are created with **New gateway token** on the
Gateways page or with `POST /api/v1/gateways`. The secret is shown once and only its SHA-256
hash is stored. A gateway id may have several tokens.

Tokens are managed on the gateway's page, not in the list. Revoking one there closes the
config and usage streams that authenticated with it at once; other tokens of the same gateway
id keep their own streams. The instance whose token was revoked keeps serving its last good
config and cannot receive a new one until it connects with a valid token.

One gateway id can run several instances (replicas). Each instance has its own config stream
and reports its own version, applied config and rate-limit setting; the Gateways page has one
compact, monitoring-only row per gateway id.

### The gateway list

| Column | What it shows |
| --- | --- |
| Gateway | The id, linked to the gateway's page, with the build version(s) its connected instances reported underneath. |
| Instances | "n/m instances connected", green when all are connected and amber when some are not. A gateway whose token exists but that has not connected yet reads "No instances yet". |
| Config | The applied version plus at most one badge, in priority order: quarantined > NACK > pending > behind. The applied version is the lowest ACKed version among the connected instances (all known instances when none is connected), so a gateway only reads as up to date when all of them are. The NACK badge carries its code and reason as a tooltip. |
| Rate limits | "Shared (namespace)" means every connected instance shares its rate-limit counters through Redis under that namespace; "Per instance" means none does; "Mixed" means the connected instances disagree on whether they share or on the namespace. |
| Last seen | The latest status write from any instance. |
| Health | "Needs attention" when the connected instances report different build versions, disagree on shared limits, or the gateway has an outstanding NACK or quarantined version. It is empty when none applies. |

The list has no expandable rows and no token actions; both live on the gateway's page. On
narrow screens the less important columns are dropped, leaving Gateway and Instances.

![Gateways list](/screenshots/gateways.png)

### The gateway page

Selecting a gateway id opens its page. A warning above the summary appears only when there is
something to fix. It states each problem in a sentence: the differing build versions, the
instances that disagree on shared limits together with the namespaces in use, the instances
that are out of step (below), the last NACK with its code and reason, and any quarantined
version. An instance that reported no build version is not out of step for that alone.

The summary shows the gateway's state, connected instances, build version(s), applied config
(with "behind" or "pending"), the full rate-limit explanation, the capabilities reported by
every connected instance, when it connected or disconnected, and when it was last seen.

**Instances** is a table with visible headers: instance id, state, build version, applied
config with any pending update or NACK, connected since, last seen and network peer. Connected
instances sort first; disconnected ones are dimmed and stay listed for 24 hours. An **Out of
step** warning marks an instance whose build version or shared-limits setting differs from the
strict majority of the connected instances. When two values are equally common — for example,
during a rolling upgrade — neither side is marked.

**Tokens** lists each token of the gateway with its label, prefix, creation time, last use and
status. **New token** opens a dialog with the gateway id already filled in, and **Revoke**
asks for confirmation before closing the streams that use that token.

![Gateway detail](/screenshots/gateway-detail.png)
