# Lumewarp

LLM gateway meant to beat LiteLLM on performance and manageability. Full design: `docs/architecture.md` (read it before any non-trivial change).

The owner, Andy, writes in Traditional Chinese — reply to him in Traditional Chinese. Code, comments, commit messages and identifiers stay in English.

## Layout (monorepo)

- `gateway/` — data plane, Rust (tokio, hyper, rustls, arc-swap). Cargo workspace.
- `control-plane/` — Python 3.12+, FastAPI, SQLAlchemy + Alembic, Authlib, grpcio. Managed with uv.
- `ui/` — Admin UI, React + Vite static SPA (TanStack Query/Router). Built output is served by the control plane; no Node at runtime.
- `proto/` — protobuf schema for the config snapshot and the config-stream gRPC service. Single source of truth for types on both sides.
- `bench/` — mock upstream (fixed latency, SSE streaming) and load scripts comparing Lumewarp vs LiteLLM.
- `deploy/` — Helm chart, Prometheus rules, Grafana dashboards.

## Non-negotiable rules

1. **Hot path stays fast.** The gateway request path never touches a database, never calls the control plane, and never runs Python. It reads only the in-memory config snapshot. Counters and usage events are emitted asynchronously.
2. **Python only in the control plane.** Customization (auth providers, router policies, notifiers) is a Python plugin registered via entry points (`lumewarp.auth`, `lumewarp.router`, `lumewarp.notifier`). Plugins produce declarative output compiled into the snapshot. Per-request hooks exist only as an opt-in external gRPC call with a timeout and a default route.
3. **Providers: OpenAI-compatible only.** Upstreams are vLLM and OpenAI (plus any OpenAI-compatible endpoint). Do not add translation layers for other providers. Pass through unknown request fields (e.g. vLLM `extra_body`, guided decoding) untouched.
4. **Config is an immutable versioned snapshot** pushed over a gRPC stream (full, then deltas; ACK/NACK). Gateway swaps atomically and persists the last good snapshot to disk so it can boot without the control plane. Gateway also supports a standalone YAML mode with the same schema.
5. **Observability is a feature, not an afterthought.** Every new gateway path gets Prometheus metrics (`lumewarp_*`, see the table in `docs/architecture.md`) and OTel spans. No API-key-level labels on metrics. Never log prompts/responses by default.
6. **Performance claims need numbers.** Any change on the gateway request path must run the `bench/` suite; don't regress p99 overhead.

## Current phase: P2 done, P3 not started

P0 (Rust gateway + bench vs LiteLLM), P1 (control plane, gRPC config stream, usage/budgets, Admin UI) and P2 are implemented; `docs/architecture.md` ("P1 已實作", "P2 已實作") lists what exists and what is deferred. P2 delivered:

- Tenancy: installation superuser → team → members (admin/member) and service accounts → API keys. No org/project.
- Admin login via OIDC (cookie sessions + CSRF); the IdP only authenticates. Authorization is pluggable (`builtin`, `openfga`, `http`, or a `lumewarp.authz` entry point) behind `check(user_id, relation, resource)`; backend errors are never denies. A key reconciler suspends keys of members removed in an external IAM.
- Gateway routing: retries (before any response byte, budgeted), fallbacks, circuit breaker, active health checks, LOWEST_LATENCY, VLLM_LOAD, traffic split with sticky hashing — each gated by a snapshot capability the control plane emits only when used.
- Observability: Prometheus alert rules (promtool-tested), Grafana dashboards, control-plane `/metrics` on its own listener.

Acceptance suites: `scripts/e2e-p1.sh`, `scripts/e2e-p2.sh` (needs `scripts/dev-idp.sh up`), `scripts/load-e2e.sh`, and `bench/` A6 for request-path changes.

Next: P3 (MCP / A2A), not planned yet.

Decided: license Apache-2.0; MVP usage data in Postgres (ClickHouse later).

