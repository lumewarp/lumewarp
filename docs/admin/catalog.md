# Upstreams, models and aliases

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

## Model types

`POST /models` requires `model_type`: `llm`, `embedding`,
`transcription` or `decision`. Models, aliases, routers and catalog entries expose
`model_type`; aliases derive it from their target, routers from their members (null
when empty). Every router member, split member and fallback must share one type.
Changing a referenced model's type returns 409 `model_type_in_use`.

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
Periodic `chat_check` and Test now probe each type's endpoint, including generated
silent WAV multipart uploads for transcription. JSON probes apply `default_body`.

Usage stores `audio_ms` and supports `endpoint` and historical, endpoint-derived
`model_type` dimensions and filters; MCP/A2A have no model type. `/users/me` retains
`models: string[]` and adds `typed_models: [{name, model_type}]` in each team.
Playground lists and proxies only LLM names, including LLM aliases and routers.
