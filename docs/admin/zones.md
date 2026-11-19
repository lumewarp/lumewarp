# Zones

A zone is one configuration boundary inside a Lumewarp installation. It is orthogonal to
identity and tenancy: orgs, teams, users, roles and grants are shared across zones, while
runtime resources and traffic are kept in a zone.

![The zone switcher](/screenshots/zone-switcher.png)

## What belongs to a zone

Each zone has its own:

- upstreams and model deployments;
- MCP servers and A2A agents;
- gateway processes, gateway tokens and config versions;
- API keys, effective rate limits, budgets, usage totals and request records.

Model, alias and router definitions—and their names and grants—are installation-wide. A
model can be deployed in one zone, several zones or none. A router or alias is also defined
once; the compiler includes it in a zone only when its targets can be served there. Orgs,
teams, users, memberships and roles are likewise global, so the same team can have different
limits and budgets in different zones without duplicating its membership.

## Switch zones

The zone switcher is at the left of the Admin UI header. It changes the zone shown on
zone-aware pages; the colour dot next to the zone's name tells zones apart. The choice is remembered per tab;
a new tab starts with the last selected zone. Global pages such as Users and model
definitions do not become zone-specific when you switch.

The switcher menu gives installation admins **Manage zones**. The Zones page is not a
sidebar item.

## Declare and configure a zone

The control plane creates the default zone named by `LUMEWARP_ZONE` at startup. It also
creates a zone for each `LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN` environment variable, where
`<ID>` is the zone ID upper-cased with `-` replaced by `_`. Each token must be at least 32
characters. The control plane reads the values at startup and keeps only their SHA-256
hashes in memory. There is no zone-creation UI or API; adding a zone means configuring its
environment token and restarting the control plane. A zone without a token is shown on the
Zones page as not configured for gateway access.

Only installation admins can edit or archive zones. Any signed-in user can see the live
zones in the switcher. A new zone starts empty: create its upstreams, add deployments for
the models it should serve, register any MCP servers or A2A agents, and connect gateways
using its token.

A zone has an immutable lowercase ID (the slug gateways pass as `--zone` and the API uses),
a name shown in the Admin UI, which can be changed, and a colour. Its settings also define key rules: the minimum creator role, the maximum key
lifetime, whether expiry is required, the maximum number of user keys per member, whether
service-account keys are allowed, and whether the Playground is allowed. Empty maximums
leave that zone without an additional cap; a team's own key limits still apply. Changing a
rule does not silently revoke existing keys; keys that no longer satisfy a tightened rule
are reported as rule violations, and current lifecycle rules are checked when a key is
created, rotated, changed or resumed.

A zone's Playground gateway URL is configured separately for each zone. The Playground
uses the URL belonging to the selected key's zone; the zone must also allow Playground use.
An empty URL makes Playground unavailable for that zone.

![The Zones page](/screenshots/zones.png)

## Archive a zone

Archiving is available from **Manage zones**. It is refused for the default zone, while any
gateway is connected, or while any key in the zone is not revoked. Disconnect the gateways
and revoke the remaining keys before trying again. The compiler stops publishing
configurations for an archived zone, and handshakes for it fail with `FAILED_PRECONDITION`
even if its environment token remains configured. The Zones page shows when an archived
zone still has a token configured. Startup does not un-archive a zone and logs a warning
when an environment token names an archived zone. The zone is retained as archived history
and cannot be edited or used as a live zone.

## Connect a gateway to a zone

A gateway serves the zone selected by `--zone`. Configure the zone's fixed token on the
control plane and provide the same secret to the gateway. For a zone named `prod`, the
control-plane variable is `LUMEWARP_GATEWAY_ZONE_PROD_TOKEN`:

```sh
export LUMEWARP_GATEWAY_ZONE_PROD_TOKEN='replace-with-a-random-secret-at-least-32-characters'

# Set the same value in the gateway's environment:
export LUMEWARP_GATEWAY_TOKEN='replace-with-a-random-secret-at-least-32-characters'
lumewarp \
  --control-plane http://control-plane:50051 \
  --gateway-token-env LUMEWARP_GATEWAY_TOKEN \
  --state-dir /var/lib/lumewarp --zone prod
```

The environment token is read by the control plane at startup. The gateway sends it with
the zone on both config and usage RPCs; `--zone` must match the token's zone. The cached
snapshot in `--state-dir` names its zone, so moving a gateway to another zone needs a new
state directory. Rotation has no grace: update both environments and restart the control
plane and gateway. The gateway keeps serving its last good snapshot while it reconnects.
The gateway then appears on the **Gateways** page of that zone. See
[Gateway](../operations/gateway.md) for the other flags.

## Keys and gateway access

The fixed gateway token authenticates exactly one zone. Any number of gateway processes may
share it, but each process serves only that zone. A client API key also belongs to exactly
one zone; its zone cannot be changed by rotation. A key sent to a gateway for another zone
is not present in that gateway's snapshot and will not authenticate. The key's zone selects
its catalog deployments, rate limits, budget and usage accounting.

There is no zone component in client request paths. Configure the client with the gateway
URL for the zone and the key created in that zone. The `/v1/models` response is the names
available to that key in that gateway's zone.

## Rollback

Config versions are listed per zone, but rollback is installation-wide. Choosing a version
restores the installation's catalog state captured by that version and publishes a new
configuration for every live zone, compiling each zone's own deployments and MCP/A2A
resources. It can therefore change production and development together. Current keys,
grants and policies are not rolled back.
