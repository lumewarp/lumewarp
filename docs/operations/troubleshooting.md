# Troubleshooting

Start with the request id. Every gateway response carries `x-lumewarp-request-id`; usage
events and traces are stored under it
([Looking up a request](./observability.md#looking-up-a-request)).

## A client gets an error

| Symptom | Likely cause | Check |
| --- | --- | --- |
| 401 `invalid_api_key` on every request | The key is expired, suspended or revoked, or the gateway has not received the config that contains it. | The key's state in the Admin UI; the gateway's acknowledged version on the Gateways page. |
| 401 right after creating a key | The new config version has not reached the gateway yet. | Normally under a second. If it persists, see "A gateway does not pick up changes". |
| 403 `model_not_allowed` | The key's effective policy does not include the name. Grants follow the called name: a key granted a router may not call its members by name. | The owner's effective models (**My keys**), and the team and org policy above it. |
| 400 `model_type_mismatch` | The name's model type does not match the endpoint. | The name's type in `GET /v1/models`. |
| 429 `rate_limit_exceeded` | A limit at org, team, owner or key level. | `lumewarp_requests_total` with status 429 for the team; the limits along the chain in the Admin UI. |
| 429 `insufficient_quota` | A monthly budget is spent. | Usage against the org, team and owner budgets. |
| 502 `upstream_error` / 504 `upstream_timeout` | The upstream failed after retries and fallbacks. | **Test now** on the upstream and the model; `lumewarp_upstream_errors_total`. |
| 503 `not_ready` | The gateway has no snapshot. | See below. |
| MCP or A2A answers 503 | `LUMEWARP_GATEWAY_ID_KEY` (and, for A2A, `LUMEWARP_GATEWAY_PUBLIC_URL`) is not set on the gateway. | The gateway's environment. |

## A gateway is not ready

In control-plane mode a gateway serves only after it has a config version. `/readyz` on its
admin listener answers 503 until then.

- **First start, control plane unreachable**: there is no cached snapshot to boot from.
  Check the `--control-plane` URL, and that the token in the named environment variable is
  unrevoked and belongs to this `--scope`.
- **Scope mismatch**: `--scope` must equal the control plane's `LUMEWARP_SCOPE`.
- **Cached snapshot rejected**: a corrupt or wrong-scope file in `--state-dir` is never
  served; the next full snapshot replaces it.

## A gateway does not pick up changes

- The Gateways page shows each gateway's acknowledged version and its last NACK.
- `UNSUPPORTED_FEATURE`: the config uses something this gateway does not have, usually a
  plugin missing from its plugin directory. Only this gateway skips the version; the others
  may apply it. Install the plugin and restart the gateway, or remove the setting.
- `INVALID_CONFIG` with a sealed upstream key: the gateway lacks `LUMEWARP_ENCRYPTION_KEY`,
  or its value differs from the control plane's.
- `lumewarp_config_stream_connected` is 0: the gateway cannot reach the control plane's
  gRPC port, or its token was revoked.

## The control plane is not ready

`GET /readyz` lists each check. The common ones:

- `database: unreachable`: check `LUMEWARP_DATABASE_URL`.
- `migrations: at X, head is Y`: run `lumewarp-control-plane migrate`.
- A background task `stopped`: the log has the reason; restart the process.

## People cannot sign in or lose access

- **Nobody is an admin yet**: set `LUMEWARP_INITIAL_ADMIN_EMAIL` and sign in with that
  email, or use the emergency login.
- **The identity provider is down**: the emergency login works without it.
- **`authz_unavailable` (503)**: the authorization backend did not answer in time. Lumewarp
  never treats that as a denial, and never suspends keys because of it.
- **A user's keys were suspended (`owner_removed`)**: they were removed from the team or
  from the backend. Restoring membership does not resume keys; the owner or a team admin
  resumes them.

## Usage is missing

- Usage arrives about a second after a request. A gateway that cannot reach the control
  plane queues up to 100,000 events in memory and drops the rest
  (`lumewarp_usage_events_dropped_total`); a gateway restart loses what was queued.
- MCP and A2A requests are recorded but have no tokens or cost.
