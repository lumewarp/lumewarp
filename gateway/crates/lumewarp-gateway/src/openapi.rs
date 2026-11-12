//! The gateway's OpenAPI 3.1 document (`lumewarp openapi`).
//!
//! It is built from the tables the server itself runs on: the proxied paths come from
//! [`ENDPOINTS`] and the error codes from [`error::CODES`]. Adding an endpoint or a code
//! therefore changes the document, and neither can be added without its description.
//!
//! The gateway passes request and response bodies through, so the document describes only
//! what the gateway itself reads, adds or refuses. The body of each proxied endpoint is an
//! open object; its fields are the upstream's.

use serde_json::{json, Map, Value};

use crate::{
    endpoint::{type_name, BodyKind, Endpoint, ENDPOINTS},
    error::{self, Scope},
};

const DESCRIPTION: &str = "\
The data plane of Lumewarp: the API that clients call. It proxies OpenAI-compatible \
requests to the model, alias or router named in the request, and proxies MCP servers and \
A2A agents.

## Authentication

`/v1/*` takes a virtual API key as `Authorization: Bearer lw-…`. The MCP and A2A endpoints \
take the same key in exactly one `X-Lumewarp-Key` header and do not accept a bearer. A \
standalone gateway configured with open authentication accepts `/v1/*` without a key; MCP \
and A2A always need one.

## Bodies pass through

A proxied request must be a JSON object (or a multipart form, for audio) with exactly one \
string `model`: the name of a model, an alias or a router. The gateway rewrites that field \
to the upstream's model name and leaves every other byte as it is, so any field the \
upstream understands works, including ones this document does not list. Responses are not \
translated either, except that `model` in the response is the name the client called. A \
stream is forwarded event by event.

## Request id

Every response carries `x-lumewarp-request-id`. Usage records and traces are stored under \
it, so it is the value to quote when asking about one request.

## Errors

An error the gateway produces is OpenAI-shaped:

```json
{\"error\": {\"message\": \"Model not found\", \"type\": \"invalid_request_error\", \"param\": null, \"code\": \"model_not_found\"}}
```

`code` is stable and is listed with each response below. An HTTP error the upstream \
answers with is passed through unchanged and does not use these codes. MCP and A2A report \
protocol errors as JSON-RPC errors inside a 200 response.

## Listeners

The primary listener serves everything except `/readyz` and `/metrics`, which are on the \
admin listener only. `/healthz` is on both.
";

const KEY_BEARER: &str = "ApiKey";
const KEY_HEADER: &str = "LumewarpKey";

/// The whole document. Deterministic: object keys are sorted by `serde_json`.
pub fn document() -> Value {
    let mut paths = Map::new();
    for endpoint in ENDPOINTS {
        paths.insert(endpoint.path.into(), json!({"post": proxied(endpoint)}));
    }
    paths.insert("/v1/models".into(), json!({"get": list_models()}));
    paths.insert("/mcp".into(), mcp(false));
    paths.insert("/mcp/servers/{server}".into(), mcp(true));
    paths.insert("/a2a".into(), json!({"get": list_agents()}));
    paths.insert("/a2a/{agent}".into(), json!({"post": a2a_rpc()}));
    paths.insert(
        "/a2a/{agent}/.well-known/agent-card.json".into(),
        json!({"get": agent_card()}),
    );
    paths.insert("/healthz".into(), json!({"get": healthz()}));
    paths.insert("/readyz".into(), json!({"get": readyz()}));
    paths.insert("/metrics".into(), json!({"get": metrics()}));

    json!({
        "openapi": "3.1.0",
        "info": {
            "title": "Lumewarp gateway",
            "version": env!("CARGO_PKG_VERSION"),
            "description": DESCRIPTION,
            "license": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
        },
        "tags": [
            {"name": "models", "description": "OpenAI-compatible endpoints. Each serves one model type; calling a name of another type answers 400 `model_type_mismatch`."},
            {"name": "mcp", "description": "Model Context Protocol, revision 2025-11-25 over Streamable HTTP."},
            {"name": "a2a", "description": "Agent2Agent protocol 1.0 over JSON-RPC and SSE."},
            {"name": "health", "description": "Liveness, readiness and metrics. No authentication."},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                KEY_BEARER: {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "lw-",
                    "description": "A virtual API key (`lw-…`). During a rotation's grace period the previous secret authenticates as the same key.",
                },
                KEY_HEADER: {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Lumewarp-Key",
                    "description": "A virtual API key (`lw-…`) in exactly one `X-Lumewarp-Key` header. A key whose budget is exhausted is still accepted here: MCP and A2A requests are not billed.",
                },
            },
            "schemas": schemas(),
            "headers": {
                "RequestId": {
                    "description": "The id this request is recorded under in usage and traces.",
                    "schema": {"type": "string", "format": "uuid"},
                },
                "RetryAfter": {
                    "description": "Seconds to wait before retrying.",
                    "schema": {"type": "integer", "minimum": 0},
                },
            },
        },
    })
}

fn schemas() -> Value {
    let codes: Vec<&str> = error::CODES.iter().map(|known| known.code).collect();
    let types: Vec<&str> = ENDPOINTS
        .iter()
        .map(|endpoint| type_name(endpoint.model_type))
        .fold(Vec::new(), |mut seen, name| {
            if !seen.contains(&name) {
                seen.push(name);
            }
            seen
        });
    json!({
        "Error": {
            "type": "object",
            "required": ["error"],
            "properties": {"error": {
                "type": "object",
                "required": ["message", "type", "param", "code"],
                "properties": {
                    "message": {"type": "string", "description": "For people; may change."},
                    "type": {
                        "type": "string",
                        "enum": ["authentication_error", "permission_error", "insufficient_quota", "rate_limit_error", "upstream_error", "invalid_request_error"],
                        "description": "The OpenAI error class, derived from the status and the code.",
                    },
                    "param": {"type": "null"},
                    "code": {"type": "string", "enum": codes, "description": "Stable; for programs."},
                },
            }},
        },
        "ProxiedRequest": {
            "type": "object",
            "required": ["model"],
            "properties": {
                "model": {"type": "string", "description": "The name of a model, an alias or a router the key is granted."},
                "stream": {"type": ["boolean", "null"], "description": "Null or absent means false."},
            },
            "additionalProperties": true,
            "description": "Every other field is forwarded to the upstream untouched.",
        },
        "ProxiedForm": {
            "type": "object",
            "required": ["model", "file"],
            "properties": {
                "model": {"type": "string", "description": "The name of a model, an alias or a router the key is granted."},
                "file": {"type": "string", "format": "binary", "description": "The audio."},
            },
            "additionalProperties": true,
            "description": "Every other part is forwarded to the upstream untouched.",
        },
        "ModelList": {
            "type": "object",
            "required": ["object", "data"],
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {"type": "array", "items": {
                    "type": "object",
                    "required": ["id", "object", "created", "owned_by", "type"],
                    "properties": {
                        "id": {"type": "string", "description": "A name the key may call: a model, an alias or a router."},
                        "object": {"type": "string", "enum": ["model"]},
                        "created": {"type": "integer", "enum": [0]},
                        "owned_by": {"type": "string", "enum": ["lumewarp"]},
                        "type": {"type": "string", "enum": types, "description": "The model type, which decides the endpoints that serve the name."},
                    },
                }},
            },
        },
        "AgentList": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "cardUrl"],
                "properties": {
                    "name": {"type": "string"},
                    "cardUrl": {"type": "string", "format": "uri", "description": "Where to fetch the agent's card."},
                },
            },
        },
        "JsonRpc": {
            "type": "object",
            "description": "A JSON-RPC 2.0 message; its methods are the protocol's, not Lumewarp's.",
            "additionalProperties": true,
        },
    })
}

/// The response entry for one status: every code of the scopes that can answer with it.
fn errors(scopes: &[Scope], statuses: &[u16]) -> Map<String, Value> {
    let mut out = Map::new();
    for status in statuses {
        let lines: Vec<String> = error::CODES
            .iter()
            .filter(|known| scopes.contains(&known.scope))
            .filter(|known| known.status == *status || known.status == 0)
            .map(|known| format!("`{}`: {}", known.code, known.meaning))
            .collect();
        assert!(!lines.is_empty(), "no error code answers {status} here");
        let mut headers =
            json!({"x-lumewarp-request-id": {"$ref": "#/components/headers/RequestId"}});
        if *status == 429 {
            headers["Retry-After"] = json!({"$ref": "#/components/headers/RetryAfter"});
        }
        out.insert(
            status.to_string(),
            json!({
                "description": lines.join("\n\n"),
                "headers": headers,
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
            }),
        );
    }
    out
}

fn with_errors(mut success: Map<String, Value>, scopes: &[Scope], statuses: &[u16]) -> Value {
    success.extend(errors(scopes, statuses));
    Value::Object(success)
}

fn ok(description: &str, content: Value) -> Map<String, Value> {
    let mut out = Map::new();
    out.insert(
        "200".into(),
        json!({
            "description": description,
            "headers": {"x-lumewarp-request-id": {"$ref": "#/components/headers/RequestId"}},
            "content": content,
        }),
    );
    out
}

fn proxied(endpoint: &Endpoint) -> Value {
    let kind = type_name(endpoint.model_type);
    let (media, schema) = match endpoint.body {
        BodyKind::Json => ("application/json", "ProxiedRequest"),
        BodyKind::Multipart => ("multipart/form-data", "ProxiedForm"),
    };
    let mut content =
        json!({"application/json": {"schema": {"type": "object", "additionalProperties": true}}});
    if endpoint.body == BodyKind::Json {
        content["text/event-stream"] = json!({"schema": {"type": "string"}});
    }
    json!({
        "tags": ["models"],
        "operationId": endpoint.label,
        "summary": endpoint.summary,
        "description": format!(
            "Serves `{kind}` names. The request is forwarded to the upstream's `{}` with `model` replaced by the upstream's model name, and the upstream's response is returned as it arrives.",
            endpoint.upstream_suffix,
        ),
        "security": [{KEY_BEARER: []}],
        "requestBody": {
            "required": true,
            "content": {media: {"schema": {"$ref": format!("#/components/schemas/{schema}")}}},
        },
        "responses": with_errors(
            ok("The upstream's response, with any status it answered. `model` is the name the client called.", content),
            &[Scope::Any, Scope::Llm],
            &[400, 401, 403, 404, 408, 413, 429, 502, 503, 504],
        ),
    })
}

fn list_models() -> Value {
    json!({
        "tags": ["models"],
        "operationId": "models",
        "summary": "List callable names",
        "description": "Every name the key may call (models, aliases and routers), sorted. Answered by the gateway from its config; the upstreams are not asked.",
        "security": [{KEY_BEARER: []}],
        "responses": with_errors(
            ok("The names.", json!({"application/json": {"schema": {"$ref": "#/components/schemas/ModelList"}}})),
            &[Scope::Any],
            &[401, 429, 503],
        ),
    })
}

fn path_parameter(name: &str, description: &str) -> Value {
    json!({
        "name": name,
        "in": "path",
        "required": true,
        "description": description,
        "schema": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,62}$"},
    })
}

fn header_parameter(name: &str, required: bool, description: &str) -> Value {
    json!({
        "name": name,
        "in": "header",
        "required": required,
        "description": description,
        "schema": {"type": "string"},
    })
}

fn json_rpc_content() -> Value {
    json!({
        "application/json": {"schema": {"$ref": "#/components/schemas/JsonRpc"}},
        "text/event-stream": {"schema": {"type": "string"}},
    })
}

fn mcp(single: bool) -> Value {
    let (id, summary, what) = if single {
        (
            "mcpServer",
            "One MCP server",
            "One upstream MCP server, with its tool and prompt names unchanged.",
        )
    } else {
        ("mcp", "The key's MCP servers as one", "One virtual MCP server made of every server the key is granted. Tool and prompt names are `<server>__<name>`, split at the first `__`.")
    };
    let mut parameters = vec![
        header_parameter(
            "MCP-Protocol-Version",
            false,
            "The negotiated revision, on every request after `initialize`.",
        ),
        header_parameter(
            "Mcp-Session-Id",
            false,
            "The session id `initialize` returned; required on every later request.",
        ),
    ];
    if single {
        parameters.insert(0, path_parameter("server", "The server's name."));
    }
    let statuses = [400, 401, 403, 404, 408, 413, 429, 502, 503, 504];
    let scopes = [Scope::Any, Scope::Mcp];
    let responses = |success: Map<String, Value>| with_errors(success, &scopes, &statuses);
    let mut item = json!({
        "parameters": parameters,
        "post": {
            "tags": ["mcp"],
            "operationId": id,
            "summary": summary,
            "description": format!("{what} Lists show only what the key is granted and what passed definition review, and a call to anything else is refused. `tools/call` counts against the key's requests per minute. A notification is answered with 202."),
            "security": [{KEY_HEADER: []}],
            "requestBody": {"required": true, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/JsonRpc"}}}},
            "responses": responses({
                let mut success = ok("A JSON-RPC response, or a stream of them. Protocol errors are JSON-RPC errors here.", json_rpc_content());
                success.insert("202".into(), json!({"description": "A notification or a response was accepted."}));
                success
            }),
        },
        "delete": {
            "tags": ["mcp"],
            "operationId": format!("{id}Delete"),
            "summary": "End the session",
            "description": "Forwarded to every upstream of the session.",
            "security": [{KEY_HEADER: []}],
            "responses": responses(ok("The session is closed.", json!({}))),
        },
    });
    if single {
        item["get"] = json!({
            "tags": ["mcp"],
            "operationId": format!("{id}Stream"),
            "summary": "Listen for server messages",
            "description": "The upstream's server-to-client stream, forwarded. Only the single-server path has one: `GET /mcp` answers 405, so a client of the aggregate re-lists instead of waiting for `list_changed`.",
            "security": [{KEY_HEADER: []}],
            "responses": responses(ok("Server messages.", json!({"text/event-stream": {"schema": {"type": "string"}}}))),
        });
    }
    item
}

fn list_agents() -> Value {
    json!({
        "tags": ["a2a"],
        "operationId": "a2aAgents",
        "summary": "List the key's agents",
        "security": [{KEY_HEADER: []}],
        "description": "The agents the key is granted, each with the URL of its card.",
        "responses": with_errors(
            ok("The agents.", json!({"application/json": {"schema": {"$ref": "#/components/schemas/AgentList"}}})),
            &[Scope::Any, Scope::A2a],
            &[401, 405, 503],
        ),
    })
}

fn a2a_rpc() -> Value {
    json!({
        "tags": ["a2a"],
        "operationId": "a2a",
        "summary": "Call an agent",
        "description": "A2A 1.0 over JSON-RPC; `SendStreamingMessage` and `SubscribeToTask` answer with a stream. Task and context ids are opaque `lwt.` values that belong to the key that received them. Push notification configuration is refused, and `ListTasks` is forwarded only for an agent whose outbound auth is `passthrough`.",
        "security": [{KEY_HEADER: []}],
        "parameters": [
            path_parameter("agent", "The agent's name."),
            header_parameter("A2A-Version", true, "Must be `1.0`; anything else is the JSON-RPC error `-32009`."),
        ],
        "requestBody": {"required": true, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/JsonRpc"}}}},
        "responses": with_errors(
            ok("A JSON-RPC response, or a stream of them. Protocol errors, an unknown or ungranted agent included, are JSON-RPC errors here.", json_rpc_content()),
            &[Scope::Any, Scope::A2a],
            &[401, 405, 503],
        ),
    })
}

fn agent_card() -> Value {
    json!({
        "tags": ["a2a"],
        "operationId": "a2aAgentCard",
        "summary": "An agent's card",
        "description": "The card as the gateway rewrites it: one JSON-RPC interface at the gateway's public URL, the `X-Lumewarp-Key` security scheme, no signatures and no push notifications.",
        "security": [{KEY_HEADER: []}],
        "parameters": [path_parameter("agent", "The agent's name.")],
        "responses": with_errors(
            ok("The Agent Card.", json!({"application/json": {"schema": {"type": "object", "additionalProperties": true}}})),
            &[Scope::Any, Scope::A2a],
            &[401, 404, 405, 502, 503],
        ),
    })
}

fn plain(description: &str, media: &str) -> Map<String, Value> {
    let mut out = Map::new();
    out.insert(
        "200".into(),
        json!({"description": description, "content": {media: {"schema": {"type": "string"}}}}),
    );
    out
}

fn healthz() -> Value {
    json!({
        "tags": ["health"],
        "operationId": "healthz",
        "summary": "Liveness",
        "description": "The process is serving HTTP. Upstreams are not probed. On both listeners.",
        "responses": plain("`ok`", "text/plain"),
    })
}

fn readyz() -> Value {
    json!({
        "tags": ["health"],
        "operationId": "readyz",
        "summary": "Readiness",
        "description": "Ready once a config snapshot is loaded. Admin listener only.",
        "responses": with_errors(plain("`ready`", "text/plain"), &[Scope::Any], &[503]),
    })
}

fn metrics() -> Value {
    json!({
        "tags": ["health"],
        "operationId": "metrics",
        "summary": "Prometheus metrics",
        "description": "OpenMetrics text. Admin listener only.",
        "responses": with_errors(
            plain("The metrics.", "application/openmetrics-text"),
            &[Scope::Admin],
            &[500],
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn operations(document: &Value) -> Vec<(String, &Value)> {
        let mut out = Vec::new();
        for (path, item) in document["paths"].as_object().unwrap() {
            for method in ["get", "post", "delete"] {
                if let Some(operation) = item.get(method) {
                    out.push((format!("{method} {path}"), operation));
                }
            }
        }
        out
    }

    #[test]
    fn is_a_valid_openapi_document() {
        use utoipa::openapi::{path::PathItem, schema::Schema, OpenApi, RefOr};
        let document = document();
        // utoipa's typed model rejects a structure that is not OpenAPI. Parts first, so a
        // failure names the schema or the path at fault.
        for (name, schema) in document["components"]["schemas"].as_object().unwrap() {
            if let Err(err) = serde_json::from_value::<RefOr<Schema>>(schema.clone()) {
                panic!("schema {name}: {err}");
            }
        }
        for (path, item) in document["paths"].as_object().unwrap() {
            if let Err(err) = serde_json::from_value::<PathItem>(item.clone()) {
                panic!("path {path}: {err}");
            }
        }
        let typed: OpenApi = serde_json::from_value(document.clone()).unwrap();
        assert_eq!(typed.paths.paths.len(), ENDPOINTS.len() + 9);
        for (name, operation) in operations(&document) {
            for field in ["operationId", "summary", "description", "responses"] {
                assert!(operation.get(field).is_some(), "{name} has no {field}");
            }
        }
    }

    #[test]
    fn every_proxied_endpoint_is_a_path() {
        let document = document();
        for endpoint in ENDPOINTS {
            let operation = &document["paths"][endpoint.path]["post"];
            assert_eq!(operation["operationId"], endpoint.label);
            assert!(!endpoint.summary.is_empty());
        }
    }

    #[test]
    fn every_error_code_is_documented_and_answered_somewhere() {
        let document = document();
        let text = document.to_string();
        let listed = document["components"]["schemas"]["Error"]["properties"]["error"]
            ["properties"]["code"]["enum"]
            .as_array()
            .unwrap();
        assert_eq!(listed.len(), error::CODES.len());
        for known in error::CODES {
            assert!(!known.meaning.is_empty());
            assert!(
                text.contains(&format!("`{}`: ", known.code)),
                "no response lists {}",
                known.code
            );
        }
    }

    #[test]
    fn operation_ids_are_unique() {
        let document = document();
        let mut ids: Vec<String> = operations(&document)
            .iter()
            .map(|(_, operation)| operation["operationId"].as_str().unwrap().to_owned())
            .collect();
        let total = ids.len();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), total);
    }
}
