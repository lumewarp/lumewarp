# Error codes

## Gateway

An error the gateway produces has an OpenAI-shaped body; `code` is stable:

```json
{"error": {"message": "Model not found", "type": "invalid_request_error", "param": null, "code": "model_not_found"}}
```

`type` is `authentication_error` for 401, `permission_error` for 403, `insufficient_quota`
and `rate_limit_error` for the two 429 codes, `upstream_error` for 502 and 504, and
`invalid_request_error` otherwise.

<!--@include: ./generated/gateway-errors.md-->

An HTTP error the upstream answers with is passed through unchanged and uses none of these
codes. MCP and A2A report protocol errors as JSON-RPC errors inside a 200 response. A
malformed HTTP request, or a header that takes too long to arrive, is answered by the HTTP
layer and need not have this shape. A failure after the response has started ends the
response; it cannot be turned into an error body.

What a client should do about each is in
[Errors, limits and retries](../using/errors.md).

## Control plane

Every non-2xx response of the management API is:

```json
{"error": {"code": "not_found", "message": "team not found"}}
```

| Status | `code` | When |
| --- | --- | --- |
| 400 | `bad_request` | The request cannot apply to this caller or state. |
| 400 | `model_type_mismatch` | The Playground was asked for a name that is not `llm`. |
| 401 | `unauthorized` | Missing or invalid credentials. |
| 401 | `invalid_credentials` | Wrong break-glass password. |
| 403 | `forbidden` | The caller may read the resource but not do this. |
| 403 | `csrf_failed` | A cookie-authenticated write without a valid `X-Lumewarp-CSRF`. |
| 404 | `not_found` | The resource does not exist, or the caller may not read it. |
| 404 | `user_not_found` | No user has the email given when adding a member. |
| 409 | `conflict` | The current state does not allow the change. |
| 409 | `managed_externally` | Memberships belong to the authorization backend. |
| 409 | `model_type_in_use` | A model's type cannot change while a router uses it. |
| 422 | `validation_error` | A field is invalid; the message names it. |
| 422 | `invalid_next` | The login's `next` is not a same-site relative path. |
| 429 | `rate_limited` | Too many key writes or login attempts; see `Retry-After`. |
| 503 | `authz_unavailable` | The authorization backend did not answer. Never treated as a denial. |
| 503 | `playground_disabled` | No gateway URL is configured for the Playground. |
| 502 | `playground_gateway_unreachable` | The Playground could not reach the gateway. |
| 502 | `upstream_unavailable` | An upstream or MCP server the control plane was asked to query (Test now, model list, Discover now) could not be reached. |
| 503 | `sso_unconfigured` | OIDC login was requested but no issuer is configured. |

The [control plane API](./control-plane-api.md) lists the statuses of each operation.
