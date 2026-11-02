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
responses and the load client's fragmented/malformed SSE handling.
