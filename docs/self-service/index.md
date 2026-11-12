# Your keys

Anyone who can sign in to the Admin UI can manage their own API keys, in every team they
belong to. A new user starts with no team: a team admin has to add you first.

## My keys

**My keys** is your home page. For each of your teams it shows:

- the names you may call, with their model type;
- your effective limits: requests per minute, tokens per minute and concurrent requests;
- what is left of this month's budget;
- how many keys you may have, and how long a key may live;
- the keys you own.

These are *your* effective values: the team's policy narrowed by your own. A member does
not see the team's full policy.

## Create a key

Choose a team, give the key a name, and optionally an expiry and limits of its own. The
secret (`lw-…`) is shown **once**; Lumewarp stores only its hash and cannot show it again.

- If the team sets a maximum key lifetime, a key without an expiry gets one at that
  maximum, and a longer expiry is refused.
- If the team limits keys per member, revoking a key frees its slot.
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

To script the management API, create a personal access token (`lwp-…`) under **Settings**.
It acts as you, with your roles. It does not call models; that is what API keys are for.

## Limits on these actions

Creating, rotating and updating keys is limited to 30 attempts per minute each. Beyond
that the API answers 429 with `Retry-After`.
