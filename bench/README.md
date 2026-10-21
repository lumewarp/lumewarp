# bench

Compares gateway overhead of Lumewarp vs LiteLLM on the same machine (P0 exit gate:
Lumewarp p99 overhead clearly below LiteLLM).

- `mock-upstream/` — OpenAI-compatible mock with fixed latency and SSE streaming (TODO, P0).
- `litellm/config.yaml` — LiteLLM pointed at the mock upstream.
- `scripts/` — load runs (oha / k6) at 1k / 5k / 10k RPS; reports overhead p50/p99, TTFT, CPU, RSS.

Any change on the gateway request path must run this suite.
