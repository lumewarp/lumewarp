# Upstreams, models and aliases

![The model list](/screenshots/models.png)

![Editing an upstream](/screenshots/upstream-edit.png)

The catalog is installation-wide. A client calls a *name*, and a name is one of three kinds,
unique across all three (`catalog_names`):

| Endpoint | What |
| --- | --- |
| `/api/v1/upstreams` | OpenAI-compatible endpoints. |
| `/api/v1/models` | A name for one model of one upstream (`upstream_id`, `upstream_model`), with its price. One try per call. |
| `/api/v1/aliases` | Another name for a model or a router (`target`, its id). One level only; not for another alias. Authorized on its own. |
| `/api/v1/routers` | A name that spreads requests over models: `strategy`, `params`, `members [{model_id, weight, params}]`. Members and fallbacks are models only. |
| `/api/v1/plugins?kind=` | Read-only, all three plugin kinds: each descriptor with its schemas and the gateways that report it. |
| `/api/v1/strategies` | **Removed** (no alias): the plugins endpoint replaced it. |
| `/api/v1/catalog` | Read-only: every name and its kind, for permission pickers. |

`/catalog` is readable by any authenticated principal; the other reads (`/upstreams`, `/models`,
`/aliases`, `/routers`, `/plugins`) need `ROUTING_READ`, which is installation-level. Writes
need `ROUTING_MANAGE`. Every write is audited and enqueues a recompile. A name that a router, alias or fallback references cannot be
deleted (409, listing who references it); deleting a name deletes the grants on it, with an
audit row. See [the architecture notes](../architecture.md#模型目錄已實作) for the reasoning.

## Credentials

An upstream sends one credential, chosen when you create it:

- **API key**: `api_key_ref`, the name of a gateway environment variable, or `api_key`,
  entered in the API or UI and stored sealed. The two are alternatives.
- **Auth plugin**: `auth: {plugin, params, timeout_ms}`, a WASM `upstream-auth` plugin.
  Only a WASM plugin is accepted here; the built-ins `none`, `static_bearer` and
  `passthrough` are for MCP servers and A2A agents, and a static bearer on an LLM upstream
  is the API key fields. It cannot be combined with an API key.

`params` are validated against the schema the gateways report for the plugin and are
stored in the snapshot and the audit trail: never put a secret there; the plugin reads
secrets from the gateway's `LUMEWARP_PLUGIN_SECRET_*`. Saving with a connected gateway that
does not report the plugin is 422 unless the write has `force`; a forced save is audited as
`upstream.force_save`, and that gateway NACKs every version that uses the plugin. The
plugin needs capability `auth:<plugin>` on the gateways that should apply the version. It
runs for every attempt (retries and fallbacks included), health checks, vLLM metric
scrapes and saved Test now; `cache-ms` keeps a cached result out of the sandbox. An attempt
whose plugin fails moves on to the router's retry and fallbacks; with none left the client
gets 502 `upstream_auth_failed`, recorded in the request log as a 502. Full contract:
[gateway plugins](../plugins.md#upstream-auth).

## Renames

`PATCH` with `name` works on all three kinds and references are by id, so
routers, aliases and grants follow. The old name is a 404 at once, except that a model's
`keep_old_name_as_alias` (the UI's default) adds an alias under the old name, with a copy of
the model's grants. `catalog_name_history` keeps old names for usage attribution; audit has
`model.rename`, `router.rename` and `alias.rename`. Metric labels switch to the new name, so
the old name's time series ends there, as usual in Prometheus. Usage rows keep the spelling of
their time and are filtered and grouped by id.

## Grants

Grants (`policy_grants`, by id) follow the called name: a key allowed a router cannot call
its members by name, a key allowed an alias cannot call its target by name, and neither
direction is implied. A router's fallback is not re-checked. The API still reads and writes
`allowed_models` as names, which must exist; a member's or service account's list must be a
subset of its team's.

A grant is a name or a [model group](#model-groups): `allowed_groups` sits next to
`allowed_models` on an org, team, member, service account or key. A policy's effective
models are the intersection down the chain of its own names plus the current members of its
groups, so a group member the level above does not allow is simply left out. `PolicyOut`
and `KeyOut` return `granted_models` (names plus group members, before the parent narrows)
next to the two lists; orgs and teams also return `effective_models` (`null` means
unrestricted), which is what the pickers below them offer.

## Model groups

A model group is a named set of models, aliases and routers granted as one unit: an
installation admin creates it on **Model groups** (Routing group in the sidebar) or with
`POST /model-groups` (`name`, `description`, `members` as catalog names). A group is never
callable, its name is not a catalog name (the page refuses a name already used by a model,
alias or router) and it cannot contain another group. Everyone signed in may read groups
(`GET /model-groups`, `GET /model-groups/{id}`) for the pickers; writes need an installation
admin.

A grant of a group stands for its members at the time, so `PATCH /model-groups/{id}` with a
new `members` list changes every holder at the next compile. Holders are not re-validated:
a member the holder's parent does not allow drops out of that holder's effective models, as
when a parent narrows. The list shows each group's member count and grants. Deleting a group
removes every grant of it (each one audited as `grant.delete`, and `model_group.delete`
records the count), so its holders lose the members at the next compile. Groups are not part
of a rollback's catalog state.

## Model types

`POST /models` requires `model_type`: `llm`, `embedding`,
`transcription` or `decision`. Models, aliases, routers and catalog entries expose
`model_type`; aliases derive it from their target, routers from their members (null
when empty). Every router member, split member and fallback must share one type.
Changing a referenced model's type returns 409 `model_type_in_use`.

## Test now

Without an auth plugin, **Test now** probes the form as it stands from the control plane:
`POST /upstreams/test` and `POST /models/test` work before you save, and a key typed into
the form is used once and not stored.

With an auth plugin, the control plane cannot run the plugin, so save first (the form
disables Test now until then). `POST /upstreams/{id}/test` and `POST /models/{id}/test`
send a probe over the config stream to a connected gateway that advertises
`upstream_probe` and the upstream's plugin; the gateway runs it against the snapshot it
currently serves. The response reports `credential: "plugin"`; a model probe returns no
reply body.

- No such gateway: 409 `no_gateway_for_plugin`.
- No answer within the upstream's timeout, capped at 15 s: 504 `probe_timeout`.
- The gateway disconnects first: 502 `probe_gateway_gone`.

The gateway probes its current snapshot, so right after a save it may answer `not_found`,
`not_found_model` or `config` until it applies the new version; retry. Such an answer is shown but not stored as the
upstream's or model's Test now result. The gateway bounds the whole probe, plugin call
included, to 15 s. Only gateways connected to the control-plane
process serving the request are candidates, so with more than one replica a request can
answer 409 even while another replica holds a capable gateway. `GET
/upstreams/{id}/models` calls the upstream directly and does not run the plugin; use a
saved upstream Test now with no model to check `/models` through a gateway instead.

Periodic `chat_check` and Test now probe each type's endpoint: LLM sends a chat
completion, embedding `input: "ping"`, decision a predicate question, and transcription a
generated silent WAV multipart upload. JSON probes apply `default_body`.

## Prices

Prices use integer nano-USD: `price_input_nano_usd_per_mtok`,
`price_output_nano_usd_per_mtok` and `price_nano_usd_per_audio_second`. All null means
unknown; only an explicitly all-zero Price is free. For a non-all-zero Price,
usage in a unit whose corresponding rates are all zero has unknown cost: both
input/output rates zero for tokens, or the audio rate zero for duration.
Transcription token rates of 0/0 alongside a nonzero audio rate therefore do not
make token usage free. LLM and decision require both token prices or
neither; embedding accepts input only, optionally with zero output. Transcription
accepts a complete token pair, an audio price, both, or neither (unknown); a partial
token pair is never allowed. Audio pricing is transcription-only; reasoning and
`max_output_tokens` are LLM-only, and transcription cannot set `default_body`.

Usage stores `audio_ms` and supports `endpoint` and historical, endpoint-derived
`model_type` dimensions and filters; MCP/A2A have no model type. `/users/me` retains
`models: string[]` and adds `typed_models: [{name, model_type}]` in each team.
Playground lists and proxies only LLM names, including LLM aliases and routers.
