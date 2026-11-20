# Config versions and gateways

Each live zone has its own immutable config versions and gateway sessions. The Admin UI
header selects the zone shown by the Config and Gateways pages. The compiler reads a zone's
upstreams, deployments, MCP servers, A2A agents, keys and limits and publishes a snapshot
only to gateways authenticated for that zone.

## Publishing

- A zone-bound change (for example an upstream, deployment, key or zone limit) queues a
  recompile for that zone. A hierarchy-wide change (such as an org, team, member, catalog
  name or grant) queues one for every live zone.
- One compiler task iterates live zones. Each zone is compiled and published under its own
  advisory lock and transaction, using that zone's outbox and source revision; work in one
  zone does not hold another zone's compile lock.
- Gateways get a full snapshot, then deltas; each update is applied, validated, persisted
  and swapped by the gateway before it ACKs. NACK `BASE_MISMATCH` triggers a full,
  `INVALID_CONFIG` quarantines the version for every gateway in that zone, and
  `UNSUPPORTED_FEATURE` makes only the gateway that lacks the capability skip it.

## Rollback

Rollback is installation-wide, even though the version you choose belongs to one zone. It
restores the catalog state captured by that version, then compiles and publishes a new
version in every live zone from that zone's own upstreams, deployments, MCP servers and A2A
agents. A model deployed in only one zone remains absent from the others. Keys, grants and
policies come from current source state, so a revoked key is not restored. Reviewed MCP
definition states are not rolled back. Restored routers, MCP servers and A2A agents go
through plugin checks; an upstream's auth plugin is restored as stored, so a gateway lacking
it NACKs the version.

![Config versions](/screenshots/config-versions.png)

## Gateways and zone tokens

A gateway process serves one zone. After accepting each config stream, the control plane
sends a `Welcome` with that stream's gateway id (`<hostname>-<8 hex>`); the hostname comes
from `Hello.hostname` and is sanitized by the control plane. Every stream gets a new id and
row, including a reconnect. Disconnected rows remain visible for 24 hours before automatic
deletion. Usage sent before a stream has welcomed the gateway has no gateway attribution.

Each zone's fixed gateway token is configured on the control plane as
`LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN`, where `<ID>` is the uppercase zone id with `-` replaced
by `_`. The token must be at least 32 characters. The control plane reads it at startup and
keeps only its SHA-256 hash in memory. The same secret is supplied to each gateway process
for its `--gateway-token-env` setting; any number of gateways may share it. There is no
gateway-token creation or revoke action in the UI or API; configure the credential through
the deployment environment. A zone without an environment token is shown as having no token
configured.

To rotate a token, change the control-plane environment value and the gateway secret to the
same new value, then restart the control plane and gateways so they load it. There is no
rotation grace. While reconnecting, gateways continue serving their last good snapshots.

![Gateways](/screenshots/gateways.png)

### The Gateways table

A warning above the table appears only when there is something to fix:

- connected gateways in this zone run different build versions;
- connected gateways disagree on shared limits, each named with its namespace or "per gateway";
- a connected gateway has an outstanding NACK, with its code and reason;
- this zone has a quarantined config version. The warning and `quarantined vN` badge appear
  only while that version is still the newest; a newer version supersedes them.

Only an `INVALID_CONFIG` NACK quarantines a version. `UNSUPPORTED_FEATURE` stays on the
NACKing gateway's row and does not withhold the version from other gateways in the zone.

| Column | What it shows |
| --- | --- |
| Gateway | The id assigned in `Welcome`, plain text. |
| State | Connected or Disconnected; disconnected rows are dimmed. |
| Version | The build version the gateway reported in its latest `Hello`. |
| Config | The ACKed version plus at most one badge: quarantined > NACK > pending > behind. A fresh gateway with no verified snapshot reads "—". |
| Rate limits | "Per gateway", or "Shared (namespace)" when it shares counters through Redis ("Shared" alone when it reports no namespace). |
| Last seen | Its latest status write. |
| Peer | The network peer of its config stream. |

A disconnected row can be deleted with `DELETE /api/v1/gateways/{zone}/{gateway_id}`.
Deleting a connected row returns 409; disconnect it first. Deletion also removes its
plugin reports and is audited. The normal 24-hour cleanup still applies.
