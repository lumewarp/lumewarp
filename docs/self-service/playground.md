# Playground

The Playground in the Admin UI sends chat requests through the real gateway with one of
your keys, or with a service-account key of a team you administer, so what you see is what
a client with that key gets: the same names, limits, budget and usage records.

![The Playground](/screenshots/playground.png)

- Pick one of your active keys, or a service-account key of a team you administer. The
  selected key is a pill; the list groups service-account keys and labels every item with
  its team and zone (and a service-account key with its account). Only `llm` names are
  listed.
- The key's zone must allow Playground use and have its own gateway URL configured. The
  Playground sends the request to that zone's URL; changing zones does not send the key to a
  different gateway.
- The request body is editable as JSON, so any field the upstream supports can be tried.
- After each reply the Playground shows the time to the first token and the tokens per
  second.
- Requests count as usage of the key you picked. A service-account key's request is
  attributed to that service account, and the [request log](../admin/requests.md) also
  records you as the Playground user.

The Playground never sees your key's secret. The control plane asks the gateway on your
behalf with a token that is valid for a few minutes and only for that key.

A Playground call is a model request, not an administrative change, so it is not an audit
entry; it appears in the request log.

If the page says the Playground is not configured, the selected key's zone either disallows
the Playground or has no gateway URL configured. Each zone's URL is managed by an
installation admin on **Manage zones**; see [Zones](../admin/zones.md#switch-zones).
