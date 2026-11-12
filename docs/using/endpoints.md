# Endpoints

The complete, generated list is the [Gateway API reference](../reference/gateway-api.md).
This page explains how the endpoints behave.

## OpenAI-compatible endpoints

| Endpoint | Serves names of type |
| --- | --- |
| `POST /v1/chat/completions` | `llm` |
| `POST /v1/completions` | `llm` |
| `POST /v1/responses` | `llm` |
| `POST /v1/embeddings` | `embedding` |
| `POST /v1/audio/transcriptions` | `transcription` |
| `POST /v1/audio/translations` | `transcription` |
| `POST /v1/decisions` | `decision` |
| `POST /v1/systemone` | `decision` |
| `GET /v1/models` | The names your key may call |

Calling a name on an endpoint of another type answers 400 `model_type_mismatch`.

## What the gateway changes, and what it leaves alone

The request must be a JSON object with exactly one string `model` field (for audio, a
multipart form with a `model` part). The gateway replaces `model` with the upstream's model
name and forwards **every other byte unchanged**: field order, whitespace and fields it has
never heard of. Whatever the upstream accepts works, including vLLM's `extra_body`
parameters and guided decoding.

The response is forwarded as it arrives. The one field the gateway rewrites is the
top-level `model`, which is always the name you called, even when a router or a fallback
chose another model.

A request body may be at most 32 MiB.

## Streaming

Set `"stream": true` as usual. Events are forwarded one by one as the upstream sends them.

To meter streamed requests the gateway may ask the upstream to include usage in the stream.
If you did not ask for it yourself (`stream_options.include_usage`), the extra usage event
is removed before it reaches you, so the stream looks exactly as you requested it.

## Settings an administrator may have put on a name

These are configured per model and explain behavior that differs between names:

- **Default body.** Fields merged under your request; your own fields win.
- **Output limit.** A request asking for more output tokens than the model allows is
  refused with 400 before it reaches the upstream.
- **Reasoning effort.** `reasoning_effort` may be forwarded, removed, or replaced by the
  request fields the upstream needs for that level.

## Headers

- Send `Authorization: Bearer lw-…`. It is replaced by the upstream's own credential; your
  key never reaches the upstream.
- Every response carries **`x-lumewarp-request-id`**. Quote it when you ask about a request:
  usage and traces are stored under it.
- `openai-organization` and `openai-project` are removed in both directions, as are
  `set-cookie` on responses and hop-by-hop headers. Other headers pass through.
- If a router is sticky by header, send the header your administrator named to stay on the
  same traffic split.

## Exact passthrough rules

For operators and client authors who need the precise contract:

- **Request body.** Collected in full, at most 32 MiB (an oversized `Content-Length` can be
  refused before reading). It must be a JSON object with exactly one string `model`;
  `stream` may be a boolean, null or absent (null and absent mean false). A duplicate
  `model` or `stream` field is refused. Error messages never echo the body.
- **What is rewritten in the request.** Only the raw value of the top-level `model`, and
  `stream_options.include_usage` on a stream the gateway meters when the client did not ask
  for usage. Every other byte is preserved: field order, whitespace, unknown fields, nested
  objects and string escapes.
- **Headers in both directions.** `connection` and the headers it names, `keep-alive`, `te`,
  `trailer`, `transfer-encoding`, `upgrade`, every `proxy-*` and `content-length` are
  removed; framing is the gateway's own.
- **Request headers.** `expect`, `openai-organization` and `openai-project` are removed.
  `Host` becomes the upstream's authority. The client's `Authorization` is replaced by the
  upstream's credential, or removed when the upstream has none.
- **Response headers.** `set-cookie`, `openai-organization` and `openai-project` are
  removed; other end-to-end headers pass through. With tracing on, `traceparent` and
  `tracestate` carry the gateway's span; with it off they pass through.
- **Response body.** Not translated. Data is forwarded as it arrives; the only event removed
  from a stream is the usage chunk the gateway asked for on the client's behalf. HTTP chunk
  boundaries are not guaranteed to match the upstream's.
- **Errors.** Gateway errors have the shape in [Errors](./errors.md). An upstream's own HTTP
  error is passed through unchanged. A failure after the response headers were sent ends the
  response; it cannot become a new status.
- Paths and methods the gateway does not serve answer 404, not 405.

## Retries are the gateway's job

When a router has a retry policy, the gateway retries a failed attempt on another member or
a fallback, but only **before any byte of the response reached you**. A stream that has
started is never restarted. So a client-side retry is still useful for errors you receive,
and should honor `Retry-After` on a 429.
