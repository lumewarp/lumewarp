# Benchmark mock upstream

Separate Rust 2021 Cargo project (not part of the gateway workspace). Requires Rust
and Cargo; no Python or external services. Intended only for trusted benchmark
traffic: authentication is ignored, request bodies are not size-limited, echo mode
reflects headers (including credentials).

```sh
cd bench/mock-upstream
cargo build --release --bins
./target/release/mock-upstream
# In another terminal:
curl -sS -H 'content-type: application/json' \
  -d '{"model":"mock","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:9000/v1/chat/completions
curl -N -H 'content-type: application/json' \
  -d '{"model":"mock","stream":true,"stream_options":{"include_usage":true}}' \
  http://127.0.0.1:9000/v1/chat/completions
```

## Example MCP server and A2A agent

`mcp-a2a-mock` (same Cargo project) serves an example MCP server and an example A2A
agent for gateway development, e2e tests and local demos. The benchmark upstream above is
unchanged; this is a separate binary with separate ports.

```sh
cd bench/mock-upstream
cargo build --release --bins
./target/release/mcp-a2a-mock mcp     # MCP   http://127.0.0.1:9110/mcp
./target/release/mcp-a2a-mock a2a     # A2A   http://127.0.0.1:9120/a2a
```

Ports 9110 and 9120 are defaults (`--listen` changes them); do not use 9000, which the
benchmark upstream (and MinIO on a dev machine) may hold. `scripts/dev.sh` starts both next
to the benchmark mock and prints their URLs.

### MCP server (revision `2025-11-25`, Streamable HTTP)

The MCP endpoint is one path (`/mcp`) and supports `POST`, `GET` and `DELETE`:

- `POST initialize` (no session) negotiates `2025-11-25` or `2025-06-18` and returns an
  `Mcp-Session-Id`. Every later message must carry that header and, after initialization,
  `MCP-Protocol-Version: <negotiated>`; an unknown session is `404`, a missing one `400`,
  and an unsupported version header `400`.
- `POST notifications/initialized` and other notifications answer `202` with no body.
- `GET /mcp` with `Accept: text/event-stream` opens the server→client stream. Every control
  call that changes the tool list (add, remove, description, reset) sends
  `notifications/tools/list_changed` there. One GET stream per session is kept; a new GET
  replaces the previous one (the spec forbids delivering the same message twice).
- `DELETE /mcp` ends the session (`204`); afterwards its id is `404`.

Tools (deterministic order, `--tools-page-size N` paginates every N with an opaque
`nextCursor`):

| Tool | Arguments | Behaviour |
| --- | --- | --- |
| `echo` | `message` | Returns the message as text. |
| `add` | `a`, `b` | Sum as text plus `structuredContent.sum`. |
| `slow` | `delay_ms`, `steps` | Sleeps per step; with `Accept: text/event-stream` it streams `notifications/progress` (when `_meta.progressToken` is set) and then the result. |
| `ask` | `question` | Sends `elicitation/create` on this POST's SSE stream, waits for the client's JSON-RPC response POST and returns its answer. |
| `whoami` | none | Returns the `Authorization`, whether `X-Lumewarp-Key` was present, the session id and all headers. |
| `admin_only` | none | Present only when the role header (`x-mock-role`, configurable) equals `admin` (configurable). |

`ask` needs the client to declare form-mode elicitation at initialize
(`capabilities.elicitation: {}` or `{"form":{}}`) and to accept SSE; otherwise it returns
`isError: true` instead of sending a request the client never agreed to receive. The mock
waits `--elicitation-timeout-ms` (default 30000) for the response POST, then returns an
`isError` result. The pending request is keyed by session and JSON-RPC id, so a response
posted with another session's `Mcp-Session-Id` is rejected with `400` — exactly the
request-id routing a proxy has to preserve.

Prompt `greet` (`prompts/list`, `prompts/get` with `arguments.name`) is also served.
Invalid tool arguments are `isError: true` results (SEP-1303); an unknown tool is `-32602`.
A `tools/call` carrying `params.task` is refused with `-32601` because the mock does not
advertise the `tasks` capability. Other flags: `--path` (default `/mcp`), `--role-header`
and `--role-admin-value` (role-dependent `admin_only`), `--prompts-page-size`,
`--allow-origin` (repeatable) and `--allow-any-origin`.

### A2A agent (v1.0, JSON-RPC + SSE)

The agent card is at `GET /.well-known/agent-card.json` (with `ETag` and
`Cache-Control`); `supportedInterfaces[0]` points at the JSON-RPC URL. Methods are the
v1.0 PascalCase names: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`,
`CancelTask`, `SubscribeToTask`. Tasks are kept in memory; `returnImmediately` returns a
working task that keeps producing updates in the background. The four push-configuration
methods and `SendMessage` with an embedded push configuration answer
`PushNotificationNotSupportedError` (`-32003`), and `GetExtendedAgentCard` is `-32004`.

A message produces a text artifact and, unless `--no-secret-artifact` is given, an artifact
whose part has `mediaType` from `--artifact-media-type` (default
`application/x-mock-secret`) — use it to test the `strip-artifacts` middleware.
`SendStreamingMessage` sends the `Task`, status updates, artifact updates and a terminal
`TASK_STATE_COMPLETED` status, then closes. `--stream-step-ms` (default 50) controls the
delay between updates; `--no-streaming` removes the streaming capability and methods;
`--no-signature` omits the dummy `signatures` entry from the card; `--public-url` sets the
base URL the card advertises.

### Authentication (per server)

| Flags | Behaviour |
| --- | --- |
| `--auth none` | Default; nothing is checked. |
| `--auth bearer --bearer TOKEN` | Requires exactly `Authorization: Bearer TOKEN`. |
| `--auth any-bearer` | Requires any non-empty `Authorization: Bearer <token>` (a passthrough user token). |
| `--auth hmac --hmac-secret SECRET` | Verifies `x-hmac-signature`; see [HMAC.md](HMAC.md). `--hmac-skew-ms` defaults to 300000. |

A `401` carries a `WWW-Authenticate` challenge. The agent card and `GET /health` stay
public; the control endpoints below are loopback-only and unauthenticated.

### Control endpoints (loopback only, path prefix `/_mock/`)

A request from a non-loopback peer gets `403`, even when the server is bound to `0.0.0.0`.
Header values are logged verbatim, secrets included; this is a test tool, not a service.

| Request | Effect |
| --- | --- |
| `GET /_mock/headers?n=20` | The last N request header sets (`at_ms`, `peer`, `method`, `path`, headers). |
| `GET /_mock/tools` (MCP) | Current tool definitions, including role-hidden ones. |
| `POST /_mock/tool` (MCP) | Add or replace a tool definition (JSON body is the tool). |
| `POST /_mock/tool/description` (MCP) | `{"name", "description"}` — the rug-pull test. |
| `POST /_mock/tool/remove` (MCP) | `{"name"}`. |
| `GET /_mock/tasks` (A2A) | In-memory tasks as rendered by `GetTask`. |
| `POST /_mock/reset` | Restore the default tools, clear the header log and tasks. |

Every MCP tool-list mutation also emits `notifications/tools/list_changed` on the active
GET stream(s).

### Registering them in the Admin UI

The MCP and A2A pages are not built yet; when they are, register each as a regular
upstream with these generic fields:

- **MCP server**: name (e.g. `mock`), URL `http://127.0.0.1:9110/mcp`, auth plugin. Use
  `none` for a public local mock, `static_bearer` with the same token as `--bearer`, or
  `hmac-signature` with the shared secret as `LUMEWARP_PLUGIN_SECRET_HMAC_SIGNATURE_KEY`
  in the gateway environment (never in the params). Review mode `off` or `tofu` is the
  convenient choice for demos.
- **A2A agent**: name (e.g. `mock-agent`), JSON-RPC URL `http://127.0.0.1:9120/a2a`,
  card URL `http://127.0.0.1:9120/.well-known/agent-card.json` (or leave it empty so the
  gateway derives it from the URL origin), same auth choices as MCP, no middleware unless
  testing `strip-artifacts`.

The gateway replaces the card's `supportedInterfaces[].url` with its own URL and drops the
upstream `signatures`; point clients at the gateway, never at these ports directly.

## Mock options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--listen` | `127.0.0.1:9000` | Socket address (IPv4 or IPv6) |
| `--latency-ms` | `50` | Async delay before headers and the first body frame (or SSE prefill with `--headers-first`) |
| `--headers-first` | off | Send SSE headers immediately, then wait `--latency-ms` before the first event |
| `--stream-chunks` | `20` | Number of content events (can be zero) |
| `--chunk-interval-ms` | `10` | Async delay between content events |
| `--echo` | off | Reflect method, path, headers and original JSON body |
| `--workers` | available parallelism | Tokio worker threads, must be positive |
| `--fail-every` | `0` (disabled) | Fail every Nth POST inference request |
| `--fail-status` | `503` | Status used by periodic failures |
| `--fail-mode` | `status` | Periodic fault kind: `status`, `reset`, or `timeout` |
| `--served-model-name` | `mock` | `model_name` label of the vLLM-style `/metrics` series |
| `--waiting` | `0` | Initial `vllm:num_requests_waiting` |
| `--kv-cache-usage` | `0` | Initial `vllm:kv_cache_usage_perc` (0..1) |
| `--large-embedding-bytes` | `0` (off) | Embeddings for model `mock` answer with one shared body of at least this size: 1536-dimension vectors, `model` after `data` |

Fault injection is opt-in: `x-mock-fail: 429` (any 400..599), `reset`
(close before headers), or `timeout` (never send headers).
`x-mock-retry-after` sets the fault response's `Retry-After` header.
`POST /fault?status=503` fails all normal requests, including health probes;
`POST /fault?status=0` restores normal behavior. The admin endpoint itself always
remains available. Defaults retain the existing benchmark responses and timing.

Load steering for `vllm_load` tests: `GET /metrics` returns vLLM-style
Prometheus text (`vllm:num_requests_waiting`, `vllm:num_requests_running` (always
0) and `vllm:kv_cache_usage_perc`, labelled `engine="0"` and `model_name`).
`POST /load?waiting=12&kv=0.8` changes the reported values (any subset), and
`POST /load?metrics_status=503` makes `/metrics` fail (`0` restores it) without
affecting inference. The response reports the current values and how many times
`/metrics` was scraped. Neither endpoint is subject to fault injection.

Endpoints: `POST /v1/chat/completions`, `/v1/completions`, `/v1/embeddings`,
`GET /v1/models`, `/health`. Unknown routes/methods return an OpenAI-shaped 404;
invalid request JSON returns 400. Embeddings contain 16 fixed float components,
with one entry per string or per array element. Responses use the requested model
(default `mock`). IDs, timestamps, text and token counts are deterministic fixtures,
not real inference or tokenization. `/health` has no simulated latency.

By default, latency precedes both response headers and body, preserving existing
benchmark behavior. With `--headers-first`, chat/completions `stream:true` requests
receive 200 + SSE headers immediately after request parsing, then wait
`--latency-ms` before any SSE event (including an echo event). This simulates
vLLM/OpenAI prefill after headers; non-stream and embedding responses are unchanged.
For example: `./target/release/mock-upstream --headers-first --latency-ms 200`.

Streaming emits exactly N content events, a stop event, optionally an empty-choices
usage event when `stream_options.include_usage` is true, then `[DONE]`. Every event
is its own Hyper body frame (network/proxy layers may combine frames). Stop, usage
and `[DONE]` have no extra delay. Echo streaming prepends an echo event; non-stream
and embedding requests in echo mode return only the echo object. Its `body` preserves
the original JSON bytes, including unknown fields and numeric representations.

The HTTP/1 server uses keep-alive and TCP_NODELAY. Default-model JSON and SSE
fragments are serialized at startup and shared as reference-counted Bytes; dynamic
model names are serialized per request. **Benchmarks must use model `mock`** to
avoid per-request payload re-serialization skewing the baseline. Requests first
validate the complete raw JSON (including UTF-8); field extraction then deserializes
only model, stream and usage options, borrowing/skipping the remaining JSON. Only
embedding requests parse `input` into a JSON tree. Zero-latency mode skips timers.

## SSE load client

```sh
./target/release/sse-bench --url http://127.0.0.1:9000/v1 \
  --model mock --concurrency 32 --requests 1000
./target/release/sse-bench --url http://127.0.0.1:9000/v1/completions \
  --endpoint completions --api-key sk-mock --concurrency 32 \
  --duration-secs 10 --out stream.json
```

`--url` is required: accepts an HTTP server root, `/v1` base or complete endpoint.
`--endpoint chat|completions` defaults to `chat`, `--model` to `mock`, concurrency
to 1; optional `--api-key` sends a bearer token. Specify exactly one positive
`--duration-secs` or `--requests`. Duration limits starting requests; in-flight
requests drain (60-second per-request timeout). The pooled client consumes each
HTTP body to EOF to allow connection reuse. HTTPS is not enabled in this local
benchmark client.

Output goes to stdout or `--out`: `requests` counts all attempts, `errors` counts
non-2xx, invalid SSE/JSON, missing content/`[DONE]`, transport failures and timeouts;
`rps` is attempts divided by wall time including drain; `ok_rps` is successful
requests (`requests - errors`) divided by that same wall time. Existing output keys
are unchanged. `ttft_ms` and `total_ms`
contain p50/p90/p99/max for successful requests only (all zero when none succeed).
TTFT means start of request through the first nonempty `delta.content` or `text`
event, not headers or a role-only chunk. Total ends at `[DONE]`. Histograms use
microsecond resolution with three significant digits. Workers maintain independent
HDR histograms and merge at the end, without shared per-request locks. SSE decoding
handles fragmented frames, CRLF, comments and multiline data. `--echo` streams are
not valid load-client responses (their first event is not a completion chunk).

## Throughput baseline

Start the mock with `--latency-ms 0` and run:

```sh
oha -z 10s -q 5000 --latency-correction --no-tui \
  -m POST -H 'content-type: application/json' \
  -d '{"model":"mock","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:9000/v1/chat/completions
```

Keep-alive is enabled by default. **oha 1.16 rejects
`--disable-keepalive=false`**; omit the flag rather than passing false. Use `-w` to
drain in-flight requests instead of counting deadline cancellations. Increase
`-q` to 10000 to verify headroom. Use a free `--listen` port if 9000 is occupied,
and match all clients/LiteLLM configuration accordingly. Performance depends on
host scheduling and concurrent benchmark/build jobs; compare on the same machine.

Validation:

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

Tests bind ephemeral loopback ports and cover response shapes, event ordering,
usage chunks, incremental delivery, raw echo preservation, embeddings, error
responses and the load client's fragmented/malformed SSE handling. `tests/mcp.rs`
and `tests/a2a.rs` drive the example servers over HTTP: initialize/sessions,
pagination, SSE progress, the `ask` elicitation round trip, GET notifications,
HMAC/bearer auth, control endpoints (including their refusal of non-loopback
peers), the agent card, streaming task updates, `ListTasks`, `CancelTask` and
`-32003` for push configuration.
