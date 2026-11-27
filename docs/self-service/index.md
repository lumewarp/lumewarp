# Your keys

Anyone who can sign in to the Admin UI can manage their own API keys in the teams they
belong to. A new user starts with no team: a team admin has to add you first. Keys belong
to one zone; use the header switcher to choose the zone before creating one. See
[Zones](../admin/zones.md) for how zones relate to teams and gateway URLs.

## My keys

![My keys: a member's keys and what each team allows them](/screenshots/my-keys.png)

**My keys** is your home page. In the selected zone, for each of your teams it shows:

- the names you may call, with their model type;
- your effective limits: requests per minute, tokens per minute and concurrent requests;
- what is left of this month's budget;
- how many keys you may have, and how long a key may live;
- the keys you own, each as a pill (hover or focus it for its org, team, owner (or
  service account), zone, prefix and id).

If you administer a team (as its team admin, or as an org or installation admin), a
**Service account keys** table below your own keys lists the keys owned by that team's
service accounts, using the same pill. Its actions are the same as the team's **Keys**
tab. A plain member does not see this table.

These are *your* effective values in the selected zone: the team's grants narrowed by your
own, and the zone's limits and budgets. A member does not see the team's full policy.

## Status banner

When a model you may call is affected, a banner under the header says so on every page:
`down`, `degraded`, `congested` or whatever the alert says, the model's name, the zone when
you have more than one, a short text and since when. When several names are affected, the banner shows the text of
the worst state and an "N names affected" button that lists them all. It lists only names
you can call in the zones where they are deployed, and disappears when the alert resolves. It reflects the
alerts your administrators route to Lumewarp ([Alerts and
incidents](../operations/alerts-and-incidents.md)); no banner means no alert is firing, not a
guarantee. Your own requests show the same failures in the error code your client received.

## Create a key

Choose a team in the selected zone, give the key a name, and optionally an expiry and
limits of its own. The key works only with gateways for that zone. The secret (`lw-…`) is
shown **once**; Lumewarp stores only its hash and cannot show it again.

![Creating a key](/screenshots/my-keys-create.png)

![The one-time secret after creating a key](/screenshots/my-keys-secret.png)

- The lower applicable zone or team maximum key lifetime applies to a user key; a zone can
  also require an expiry.
- The lower applicable zone or team per-member key cap is counted within the zone; revoking
  a key frees its slot.
- A key's own limits can only narrow yours.

## Rotate

Rotating gives the key a new secret. Choose a grace period (up to 24 hours) to keep the old
secret working while you roll the new one out; without one, the old secret stops at once.
Rotating again during the grace ends the older secret immediately.

## Suspend, resume, revoke

- **Suspend** stops a key until you resume it.
- **Revoke** is permanent.
- You can resume a key you suspended yourself, and one that was suspended because you were
  removed from the team once you are back in it. A key suspended by an administrator, or
  because your account was deactivated, needs an administrator.

## Personal access tokens

To script the management API, create a personal access token (`lwp-…`) under **Personal access tokens** at the bottom of **My keys**.
It acts as you, with your roles, and expires after 90 days unless you pick another expiry (30 days, or none) when you create it. It does not call models; that is what API keys are for.
The same token connects an MCP client to the control plane; see [Use Lumewarp from an MCP
client](./mcp-client.md).

## Limits on these actions

Creating, rotating and updating keys is limited to 30 attempts per minute each. Beyond
that the API answers 429 with `Retry-After`.
