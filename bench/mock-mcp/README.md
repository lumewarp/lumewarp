# mock-mcp

A controllable MCP upstream for Lumewarp's tests and benchmarks. One process serves
any number of named server instances at `/s/<name>/mcp`, so a test can have many
upstreams without many ports. Every instance can be modern (2026-07-28) or legacy
(2025-11-25), answer in JSON or SSE, change its tools while running, inject a fault,
and record exactly what it received.

Not for production. The admin API is unauthenticated, request bodies are held in
memory, and the recording is bounded only by `--record-capacity` and
`--record-body-limit`.

```sh
cd bench/mock-mcp
cargo build --release --bin mock-mcp
./target/release/mock-mcp --listen 127.0.0.1:9201
```

```sh
curl -sS -X POST http://127.0.0.1:9201/admin/servers \
  -H 'content-type: application/json' \
  -d '{"name":"demo","era":"modern","response":"json",
       "tools":[{"name":"echo","behaviour":"echo"}]}'
curl -sS -X POST http://127.0.0.1:9201/s/demo/mcp \
  -H 'content-type: application/json' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' -H 'Mcp-Name: echo' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"echo","arguments":{"message":"hi"},
                 "_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
                          "io.modelcontextprotocol/clientCapabilities":{}}}}'
```

## Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--listen` | `127.0.0.1:9201` | Socket address. Tests use an ephemeral port. |
| `--workers` | available parallelism | Tokio worker threads. |
| `--no-record` | off | Disable request recording entirely. The benchmark path. |
| `--record-capacity` | `2048` | Records kept; the oldest is evicted first. `0` disables recording. |
| `--record-body-limit` | `65536` | Bytes of each request body kept in a record. |
| `--max-body-bytes` | `16777216` | Request body limit; larger requests get `413`. |

## Admin API

All bodies are JSON. Errors are `{"error": "..."}` with `400`, `404` or `409`.

| Method and path | Meaning |
| --- | --- |
| `GET /health` | `{"status":"ok","servers":n,"records":n,"recording":bool}` |
| `GET /admin/servers` | `{"servers":[...]}` |
| `POST /admin/servers` | Create an instance; full config in the body; `201` |
| `GET /admin/servers/{name}` | The full config |
| `PUT /admin/servers/{name}` | Replace the instance, including its tools |
| `DELETE /admin/servers/{name}` | Delete the instance |
| `PUT /admin/servers/{name}/tools` | Replace the tool list: `{"tools":[...]}` |
| `POST /admin/servers/{name}/tools` | Upsert one tool |
| `PUT /admin/servers/{name}/tools/{tool}` | Upsert one tool, name from the path |
| `DELETE /admin/servers/{name}/tools/{tool}` | Remove one tool |
| `POST /admin/servers/{name}/fault` | `{"fault": {...}}` or a bare fault object; `{"fault":null}` clears |
| `DELETE /admin/servers/{name}/fault` | Clear the instance fault |
| `GET /admin/records` | Query records; see below |
| `DELETE /admin/records?server={name}` | Clear all records, or one server's |
| `GET /admin/recording` | `{"enabled":bool}` |
| `PUT /admin/recording` | `{"enabled":bool}` |

A server config:

```json
{
  "name": "alpha",
  "era": "modern",
  "response": "json",
  "auth": {"kind": "none"},
  "allowed_origins": ["http://localhost:3000"],
  "ttl_ms": 60000,
  "cache_scope": "public",
  "session": "optional",
  "get_stream": false,
  "instructions": null,
  "tools": [],
  "fault": null
}
```

| Field | Values | Default |
| --- | --- | --- |
| `era` | `modern` (2026-07-28), `legacy` (2025-11-25) | `modern` |
| `response` | `json`, `sse` | `json` |
| `auth` | `{"kind":"none"}` or `{"kind":"bearer","token":"..."}` | none |
| `allowed_origins` | exact `Origin` values; an absent `Origin` is always allowed | empty (present origins are refused) |
| `ttl_ms`, `cache_scope` | returned on `server/discover` and `tools/list` | `60000`, `public` |
| `session` | legacy only: `off`, `optional`, `required` | `optional` |
| `get_stream` | legacy only: answer `GET` with a standalone SSE stream instead of `405` | `false` |
| `instructions` | optional text in `server/discover` and `initialize` | absent |

A tool:

```json
{
  "name": "echo",
  "description": null,
  "input_schema": {"type": "object"},
  "definition": null,
  "behaviour": "echo",
  "result": null,
  "error": null,
  "sleep_ms": 0,
  "progress_events": 0,
  "progress_interval_ms": 0,
  "response": null,
  "fault": null
}
```

`definition`, when present, is served verbatim in `tools/list`; this is how a test
changes a tool definition without changing its name (a rug pull). Otherwise the
mock builds `{"name", "description", "inputSchema"}`. `param_headers` are derived
from `inputSchema` with the specification's statically-reachable `properties` walk,
including nested properties and the integer safe-range check. Unknown or invalid
`x-mcp-header` annotations are served but not enforced, so the control plane's
discovery validator has something to reject. A tool definition with an invalid
annotation is served unchanged; the mock only enforces annotations it can derive.

`behaviour` decides a call:

| Behaviour | Result |
| --- | --- |
| `echo` | `content[0].text` is the arguments JSON; `structuredContent` is the arguments |
| `fixed` | `result` verbatim, or a small fixed text |
| `tool_error` | `isError: true` with `error.message` |
| `protocol_error` | JSON-RPC error `error.code`/`error.message` |
| `sleep` | sleeps `sleep_ms`, then returns a text result |
| `progress` | emits `progress_events` progress notifications, then a text result |
| `mrtr` | modern: elicitation round; legacy: a tool error saying MRTR needs 2026-07-28 |

Every tool can also set `sleep_ms`, `progress_events`, `progress_interval_ms` and a
per-tool `response`/`fault`; those combine with the behaviour. Progress is emitted
only when the request supplied a `progressToken`, and forces SSE.

## Faults

A fault can be set on an instance (applies to every request to it) or on a tool
(applies to that tool's calls, overriding the instance fault).

| Kind | Effect |
| --- | --- |
| `oversized_sse_event` | one SSE event of `bytes` characters (default 17 MiB), then close |
| `compressed` | a gzip-compressed JSON body with `Content-Encoding: gzip` |
| `invalid_sse` | a truncated SSE `data:` line, then close |
| `stalled_stream` | SSE headers + one comment, then no more body |
| `slow_reads` | `events` comments spaced by `interval_ms`, then the result |
| `reset_mid_stream` | `after_events` comments, then an abortive body error |
| `wrong_content_type` | the JSON body with `content_type` (default `text/plain`) |
| `unauthorized` | `401` with `www_authenticate` |
| `server_error` | `status` (default `503`) and a small error body |
| `session_404_once` | legacy: the next request with a valid session gets `404` and the session is expired |

`reset_mid_stream` aborts the HTTP body; it is an incomplete response rather than a
guaranteed TCP RST. `session_404_once` applied to an instance is one-shot; applied
to a tool it expires the current session on every call.

## Protocol behavior

**Modern (2026-07-28).** No session and no `initialize`. `GET` and `DELETE` answer
`405`. Every request needs `_meta` (`io.modelcontextprotocol/protocolVersion` and
`io.modelcontextprotocol/clientCapabilities`), `MCP-Protocol-Version`, `Mcp-Method`
and, for `tools/call`, `Mcp-Name`. Header/body mismatches answer `400` with `-32020`;
a missing `_meta` field answers `400` with `-32602`; an unsupported version answers
`400` with `-32022` and `data.supported`; an unknown method answers `404` with
`-32601`. `server/discover` reports only this mock's own versions, capabilities and
identity. Every result has `resultType`; `tools/list` and `server/discover` carry
`ttlMs` and `cacheScope`; `tools/call` results do not. `Mcp-Name` and
`Mcp-Param-*` accept the `=?base64?...?=` sentinel; known parameter headers must be
present exactly when the argument is present and non-null, and duplicate
recognized headers are rejected. Unknown `Mcp-Param-*` headers are ignored.
Closing an SSE response stream cancels the call (a `notifications/cancelled` POST
is also honored as a courtesy). Only an elicitation the client declared is
requested; a client without `elicitation` gets `400` and `-32021`.

**Legacy (2025-11-25).** `initialize` negotiates 2025-11-25 and mints
`Mcp-Session-Id` unless `session` is `off`. Subsequent requests may carry the
session; an unknown one is `404` and a missing one is `400` when `session` is
`required`. `notifications/initialized` answers `202`. `tools/list` and
`tools/call` results have no `resultType`, `ttlMs` or `cacheScope`. `GET` answers
`405` unless `get_stream` is on; `DELETE` answers `405`. A disconnect is **not**
cancellation: the call continues, the record says `disconnected` and
`completed_after_disconnect`, and `notifications/cancelled` is the only way to stop
it.

## Records

Every `/s/<name>/mcp` request is recorded with its HTTP method, path, every header
value (including `Authorization`), the raw body (up to the body limit), the
JSON-RPC method/id/tool, session, era, response status/content type/outcome, and
whether the client cancelled or disconnected. Admin requests are not recorded.

```sh
# All records for one server and tool
curl -sS 'http://127.0.0.1:9201/admin/records?server=demo&tool=echo'
# Only records after a sequence number, at most 10
curl -sS 'http://127.0.0.1:9201/admin/records?server=demo&after=4&limit=10'
# Filter by the JSON-RPC method or the HTTP method
curl -sS 'http://127.0.0.1:9201/admin/records?method=tools%2Fcall&http_method=POST'
# Clear
curl -sS -X DELETE 'http://127.0.0.1:9201/admin/records?server=demo'
```

Filters: `server`, `http_method`, `method` (JSON-RPC), `tool`, `after`, `limit`.
The ring keeps the last `--record-capacity` records; bodies are truncated to
`--record-body-limit` bytes and `body_truncated` says so. `--no-record` and
`PUT /admin/recording {"enabled":false}` stop recording; admin reads still work.

## Tests and fixture

`tests/http.rs` is the contract test: both eras, every header rejection, parameter
headers, progress, MRTR, cancellation, every fault, sessions, recording and the
admin API.

`tests/e2e/mcp_mock.py` starts the release binary on a free port and exposes a typed
client (`Server`, `Tool`, `Fault`, `RequestRecord`) to pytest. It builds the binary
if it is missing; set `E2E_MCP_MOCK_BIN` to use one built elsewhere. The fixture
never uses port 9000 or the LLM benchmark's 9100, 4100 or 4101.

## Performance

With recording off (`--no-record`), release build, 20-core host, `oha` at A6's
offered rate (1000 RPS, 500 connections, `--latency-correction`):

| Shape | Achieved RPS | p99 |
| --- | --- | --- |
| JSON `tools/call` | 999.82 | 0.81 ms |
| SSE `tools/call` | 1000.17 | 0.68 ms |

Saturation on the same host (`-z 5s -c 500`, no rate limit): 147,856 RPS JSON and
127,755 RPS SSE, p99 9.4 ms and 11.7 ms. The mock is far above the gateway's A6
rate, so it is not the bottleneck in a mixed run; a saturated run measures the
mock or the client loop, not Lumewarp.
