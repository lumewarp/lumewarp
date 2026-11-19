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

## Gateways and tokens

A gateway is one process and serves exactly one zone. It generates its id at startup
(`<hostname>-<8 hex>`), sends it as `Hello.gateway_id`, and keeps it until it exits; a
restart is a new gateway. The Gateways page shows the selected zone's processes, connected
first; a disconnected row stays for 24 hours and is then deleted.

A gateway token (`lwg-…`) authenticates one zone, not a gateway id: any number of gateway
processes may share it. Create one with **New gateway token** on the Gateways page, with
`POST /api/v1/gateway-tokens` and its `zone` field, or with
`lumewarp-control-plane bootstrap [--name <label>] [--zone <name>]`. The control plane
creates the default zone at startup, so a fresh install can create its first token in the
UI without running `bootstrap`. The secret is shown once and only its SHA-256 hash is
stored. Revoking a token closes every config and usage stream authenticated with it; those
gateways keep serving their last good snapshots but cannot receive another update until
they reconnect with a valid token.

![Gateways](/screenshots/gateways.png)

### The Gateways tab

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
| Gateway | `Hello.gateway_id`, plain text. |
| State | Connected or Disconnected; disconnected rows are dimmed. |
| Version | The build version the gateway reported in its latest `Hello`. |
| Config | The ACKed version plus at most one badge: quarantined > NACK > pending > behind. A fresh gateway with no verified snapshot reads "—". |
| Rate limits | "Per gateway", or "Shared (namespace)" when it shares counters through Redis ("Shared" alone when it reports no namespace). |
| Token | The label of the token its latest `Hello` authenticated with, or its prefix when it has no label. |
| Last seen | Its latest status write. |
| Peer | The network peer of its config stream. |

### The Tokens tab

The selected zone's token list shows Label, Prefix, Created, Last used, Gateways, Status
and Revoke. **Gateways** is the number currently connected with that token. Revoked tokens
stay listed with their revocation time. **New gateway token** asks for a label and shows
the secret once. **Revoke** asks for confirmation, naming how many connected gateways it
disconnects and that they keep serving their last good config.
