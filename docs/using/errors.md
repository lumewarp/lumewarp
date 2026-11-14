# Errors, limits and retries

## Two kinds of error

1. **The gateway refused or could not complete the request.** The body is OpenAI-shaped
   and `error.code` is one of the codes below:

   ```json
   {"error": {"message": "Model not found", "type": "invalid_request_error", "param": null, "code": "model_not_found"}}
   ```

2. **The upstream answered with an error.** Its status and body are passed through
   unchanged, so they look like that upstream's errors, not like the table below.

Every response, of either kind, carries `x-lumewarp-request-id`.

## Codes

<!--@include: ../reference/generated/gateway-errors.md-->

## What to do about each

| You get | Meaning | Client action |
| --- | --- | --- |
| 400 | The request is malformed, or the name is of another model type than the endpoint. | Fix the request. |
| 401 `invalid_api_key` | The key is missing, unknown, expired, suspended or revoked. | Check the key under **My keys**. Do not retry. |
| 403 `model_not_allowed` | The name exists but your key is not granted it. | Ask a team admin. `GET /v1/models` lists what you may call. |
| 404 `model_not_found` | Nothing has that name. | Check the spelling against `GET /v1/models`. |
| 429 `rate_limit_exceeded` | A requests-per-minute, tokens-per-minute or concurrency limit is used up. | Wait `Retry-After` seconds, then retry. |
| 429 `insufficient_quota` | The month's budget is spent. | Do not retry; ask for a higher budget. |
| 502, 504 | The upstream failed or timed out after the gateway's own retries. | Retry with backoff. |
| 503 | The gateway has no configuration yet, or the model is recovering from failures. | Retry with backoff. |

## Limits

A key can be limited in requests per minute, tokens per minute and concurrent requests, at
four levels: its org, its team, its owner and the key itself. The lowest value applies. The
Admin UI's **My keys** page shows your effective limits.

Limits are counted in each gateway. If the installation runs several gateways without a
shared Redis, the total you can reach is higher than the configured number.

## Budgets

A budget is an amount per UTC calendar month, checked at the org, the team and the key's
owner. Spend is added up from usage reports a few seconds after each request, so the cut-off
is not exact to the request: a key can overshoot slightly, and a request already streaming
is never cut.
