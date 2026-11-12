"""Descriptions of request fields, for the OpenAPI document.

The same field appears in many request bodies (`rpm_limit` in eleven), so the text lives
here once instead of in every `Field(...)`. `describe` is the `json_schema_extra` of the
schema base classes: it fills in a property that has no description of its own, looking
up `(model, field)` first, through the model's bases, and then the field name alone. A
description written on the field itself always wins. Response models (`*Out`,
`*Created`) are left alone.

`tests/api/test_openapi.py` fails when a request field ends up with no description.
"""

from typing import Any

_LIMIT_SCOPE = (
    " at this level; null = no limit here. A lower level may not exceed a higher one"
    " (org, team, owner, key)."
)
_ACCESS = (
    "`inherit` keeps the parent's set, `all` grants everything the parent allows, `list`"
    " grants exactly `{rows}`."
)
_CALLABLE_NAME = (
    "The name clients call, without whitespace. Models, aliases and routers share one namespace."
)
_PLUGIN_TIMEOUT = (
    "WASM plugin only: how long one call may run, in ms; 0 = the gateway default (60 s)."
)
_PRICE_PAIR = " Set the input and output prices together, or leave both null (cost unknown)."

BY_NAME: dict[str, str] = {
    # Policy
    "model_access": _ACCESS.format(rows="allowed_models"),
    "allowed_models": (
        "Callable names (models, aliases, routers) granted when `model_access` is `list`;"
        " empty denies all."
    ),
    "mcp_access": _ACCESS.format(rows="mcp_servers"),
    "mcp_servers": "MCP server grants used when `mcp_access` is `list`; empty denies all.",
    "a2a_access": _ACCESS.format(rows="a2a_agents"),
    "a2a_agents": "A2A agent names granted when `a2a_access` is `list`; empty denies all.",
    "rpm_limit": "Requests per minute" + _LIMIT_SCOPE,
    "tpm_limit": "Tokens per minute" + _LIMIT_SCOPE,
    "max_parallel": "Concurrent requests" + _LIMIT_SCOPE,
    "budget_nano_usd": (
        "Budget per UTC calendar month in nano-USD (1e-9 USD); null = no budget at this level."
    ),
    "max_keys_per_member": (
        "How many keys one member may own in the team; null = unlimited. Service-account"
        " keys do not count."
    ),
    "max_member_key_lifetime_seconds": (
        "Longest lifetime of a member's key, checked on create, rotate and expiry changes;"
        " null = unlimited."
    ),
    "member_rpm_default": (
        "Requests per minute for each member who has no limit of their own; capped by the"
        " team's limit. null = none."
    ),
    "member_tpm_default": (
        "Tokens per minute for each member who has no limit of their own; capped by the"
        " team's limit. null = none."
    ),
    "member_max_parallel_default": (
        "Concurrent requests for each member who has no limit of their own; capped by the"
        " team's limit. null = none."
    ),
    # Tenancy
    "name": "Display name, 1 to 200 characters.",
    "display_name": "Shown in the Admin UI.",
    "description": "Free text.",
    "role": "`admin` or `member`.",
    "expires_at": "When it stops authenticating, with a timezone; null = no expiry.",
    # Upstreams
    "kind": "`openai`, `vllm` or `openai_compatible`. All three are proxied the same way.",
    "base_url": (
        "Absolute http(s) base URL of the upstream API, including `/v1` when that is its prefix."
    ),
    "api_key_ref": (
        "Name of an environment variable on the gateway that holds the upstream bearer"
        " token. Mutually exclusive with `api_key`."
    ),
    "stream_usage_mode": (
        "`include_usage`: the gateway asks the upstream for a usage chunk on streams, to"
        " meter them, and hides it from clients that did not ask. `passthrough`: the"
        " upstream does not support that."
    ),
    "health_url": (
        "URL of the active health probe; null = `base_url` without a trailing `/v1`, plus"
        " `/health`."
    ),
    "health_check": "Active health probe of the upstream; null = none.",
    "circuit_breaker": "Circuit breaker, kept per model of the upstream; null = none.",
    "metrics_url": (
        "Prometheus endpoint scraped for `vllm_load` routers; null = `base_url` without a"
        " trailing `/v1`, plus `/metrics`."
    ),
    "interval_ms": "Time between probes in ms, at least 1000.",
    # Models
    "model_type": (
        "`llm`, `embedding`, `transcription` or `decision`. It decides which endpoints may"
        " call the name."
    ),
    "upstream_id": "The upstream that serves the model.",
    "upstream_model": "The model name sent to the upstream.",
    "price_input_nano_usd_per_mtok": ("Price of a million input tokens in nano-USD." + _PRICE_PAIR),
    "price_output_nano_usd_per_mtok": (
        "Price of a million output tokens in nano-USD." + _PRICE_PAIR
    ),
    "price_nano_usd_per_audio_second": (
        "Transcription models only: price of one second of audio in nano-USD."
    ),
    "default_body": "Merged under the request body, as on a saved model.",
    # Routers
    "strategy": (
        "A built-in (`weighted`, `least_in_flight`, `lowest_latency`, `vllm_load`) or a"
        " WASM strategy listed by `GET /plugins`."
    ),
    "members": "The member models, in order.",
    "model_id": "A model of the router's model type.",
    "fallback_model_ids": "Models tried in order once the members failed.",
    "retry": "Retry policy; null = one attempt.",
    "traffic_split": "Named, weighted pools of members. When set, `members` is empty.",
    "sticky": (
        "Keep a caller on one traffic split: by a request `header`, by `team` or by `key`."
        " `none` draws by weight on every request."
    ),
    "sticky_header": "Header whose value is hashed; required exactly when `sticky` is `header`.",
    "plugin_timeout_ms": (
        "WASM strategy only: how long one pick may run, in ms; 0 = the gateway default (60 s)."
    ),
    "target": "Id of a model or a router, not another alias.",
    "params": "Settings the plugin defines, validated against the schema it declares.",
    # MCP and A2A
    "auth": "Outbound authentication towards the upstream; replaced as a whole.",
    "plugin": "A plugin name listed by `GET /plugins`.",
    "secret_ref": (
        "`static_bearer` only: name of an environment variable on the gateway that holds"
        " the token. Mutually exclusive with `secret`."
    ),
    "secret": (
        "`static_bearer` only: the bearer token, stored sealed with"
        " `LUMEWARP_ENCRYPTION_KEY` and never returned. Mutually exclusive with `secret_ref`."
    ),
    "review_mode": (
        "`tofu` serves a definition seen for the first time and withholds a changed one"
        " until approved; `manual` withholds both; `off` serves everything."
    ),
    "card_url": (
        "Where gateways fetch the Agent Card; null = the origin of `url` plus"
        " `/.well-known/agent-card.json`."
    ),
    "card_json": "A pasted Agent Card (JSON object, at most 64 KiB), served until a gateway"
    " fetched one.",
    "middleware": "One WASM A2A middleware plugin applied to this agent; null = none.",
    "server": "Name of an MCP server.",
    "all_tools": "Grant every approved tool of the server.",
    "tools": (
        "Upstream tool names, without the `<server>__` prefix; ignored when `all_tools` is true."
    ),
}

BY_MODEL: dict[tuple[str, str], str] = {
    ("TeamCreate", "name"): "Team name, unique among live teams.",
    ("TeamUpdate", "name"): "Team name, unique among live teams.",
    ("TeamCreate", "org_id"): "The org the team belongs to. It cannot be changed later.",
    ("MemberCreate", "user_id"): "The user to add. Give exactly one of `user_id` and `email`.",
    ("MemberCreate", "email"): (
        "Email of an existing user. Give exactly one of `user_id` and `email`."
    ),
    ("ServiceAccountUpdate", "is_active"): (
        "false deactivates the account, and its keys stop authenticating; true reactivates it."
    ),
    ("UserCreate", "email"): "The email the identity provider reports for the user.",
    ("UserUpdate", "email"): "The email the identity provider reports for the user.",
    ("UserCreate", "is_active"): "false creates the user deactivated: they cannot sign in.",
    ("UserUpdate", "is_active"): (
        "false deactivates the user: they cannot sign in and their keys are suspended."
    ),
    ("InstallationAdminAssign", "role"): (
        "Only `admin` can be assigned; `proxy_admin` moves through transfer."
    ),
    ("InstallationAdminTransfer", "user_id"): "The user who becomes `proxy_admin`.",
    ("KeyCreate", "name"): "A label for the key.",
    ("KeyUpdate", "name"): "A label for the key.",
    ("KeyCreate", "expires_at"): (
        "When the key stops authenticating: in the future, with a timezone. null = no"
        " expiry, except that a member's key gets the team's"
        " `max_member_key_lifetime_seconds` as its expiry and may not outlive it."
    ),
    ("KeyCreate", "service_account_id"): (
        "The service account that owns the key; omit for a key owned by the caller."
    ),
    ("KeyUpdate", "lifecycle"): (
        "`active` resumes the key and `suspended` suspends it. `revoked` is refused here."
    ),
    ("KeyRotate", "grace_seconds"): (
        "How long the previous secret keeps authenticating, at most 86400; 0 replaces it at once."
    ),
    ("TokenCreate", "name"): "A label for the token.",
    ("GatewayTokenCreate", "name"): "A label for the token.",
    ("GatewayTokenCreate", "gateway_id"): (
        "The gateway id the token is bound to. A gateway must present the same id."
    ),
    ("RollbackCreate", "version"): "The config version to restore.",
    ("UpstreamCreate", "name"): "Unique upstream name.",
    ("UpstreamUpdate", "name"): "Unique upstream name.",
    ("UpstreamCreate", "timeout_ms"): (
        "Time allowed until the upstream's response headers arrive, in ms; 0 = the gateway"
        " default (600000). It does not limit the response body or a stream."
    ),
    ("UpstreamUpdate", "timeout_ms"): (
        "Time allowed until the upstream's response headers arrive, in ms; 0 = the gateway"
        " default (600000). It does not limit the response body or a stream."
    ),
    ("ChatCheckIn", "timeout_ms"): "Must be below interval_ms.",
    ("ModelCreate", "name"): _CALLABLE_NAME,
    ("ModelUpdate", "name"): _CALLABLE_NAME,
    ("AliasCreate", "name"): _CALLABLE_NAME,
    ("AliasUpdate", "name"): _CALLABLE_NAME,
    ("RouterCreate", "name"): _CALLABLE_NAME,
    ("RouterUpdate", "name"): _CALLABLE_NAME,
    ("TrafficSplitIn", "members"): "The models of this split.",
    ("TargetAuthIn", "plugin"): (
        "`none`, `static_bearer`, `passthrough`, or a WASM upstream-auth plugin listed by"
        " `GET /plugins`."
    ),
    ("TargetAuthIn", "timeout_ms"): _PLUGIN_TIMEOUT,
    ("PluginRefIn", "plugin"): "A WASM A2A middleware plugin listed by `GET /plugins`.",
    ("PluginRefIn", "timeout_ms"): _PLUGIN_TIMEOUT,
    ("McpServerCreate", "name"): (
        "Lower-case name used in `/mcp/servers/<name>` and as the `<name>__` prefix of its"
        " tools; it must not contain `__`."
    ),
    ("McpServerUpdate", "name"): (
        "Lower-case name used in `/mcp/servers/<name>` and as the `<name>__` prefix of its"
        " tools; it must not contain `__`."
    ),
    ("McpServerCreate", "url"): "URL of the upstream MCP server.",
    ("McpServerUpdate", "url"): "URL of the upstream MCP server.",
    ("McpServerCreate", "timeout_ms"): (
        "Time allowed for one upstream request, in ms; 0 = the gateway default (30 s)."
    ),
    ("McpServerUpdate", "timeout_ms"): (
        "Time allowed for one upstream request, in ms; 0 = the gateway default (30 s)."
    ),
    ("McpDiscoverIn", "url"): "URL of the MCP server to query.",
    ("McpDiscoverIn", "timeout_ms"): "Time allowed for the discovery call, in ms.",
    ("A2aAgentCreate", "name"): "Lower-case name used in `/a2a/<name>`.",
    ("A2aAgentUpdate", "name"): "Lower-case name used in `/a2a/<name>`.",
    ("A2aAgentCreate", "url"): "URL of the upstream agent.",
    ("A2aAgentUpdate", "url"): "URL of the upstream agent.",
    ("A2aAgentCreate", "timeout_ms"): (
        "Time allowed for one upstream request, in ms; 0 = the gateway default."
    ),
    ("A2aAgentUpdate", "timeout_ms"): (
        "Time allowed for one upstream request, in ms; 0 = the gateway default."
    ),
}


# Responses reuse some request field names with another meaning (an effective limit, a
# callable name), so the shared text is for requests only.
RESPONSE_SUFFIXES = ("Out", "Created")


def describe(schema: dict[str, Any], model: type) -> None:
    """`json_schema_extra`: describe the properties that carry no description of their own."""
    if model.__name__.endswith(RESPONSE_SUFFIXES):
        return
    names = [base.__name__ for base in model.__mro__]
    for field, prop in schema.get("properties", {}).items():
        if prop.get("description"):
            continue
        text = next(
            (BY_MODEL[(name, field)] for name in names if (name, field) in BY_MODEL),
            BY_NAME.get(field),
        )
        if text is not None:
            prop["description"] = text
