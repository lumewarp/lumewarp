# Lumewarp

LLM gateway meant to beat LiteLLM on performance and manageability. Full design: `docs/architecture.md` (read it before any non-trivial change).

The owner, Andy, writes in Traditional Chinese — reply to him in Traditional Chinese. Code, comments, commit messages and identifiers stay in English.

## Layout (monorepo)

- `gateway/` — data plane, Rust (tokio, hyper, rustls, arc-swap). Cargo workspace.
- `control-plane/` — Python 3.12+, FastAPI, SQLAlchemy + Alembic (one squashed baseline migration; Lumewarp has never been deployed, so keep no upgrade path), Authlib, grpcio. Managed with uv.
- `ui/` — Admin UI, React + Vite static SPA (TanStack Query/Router), components are shadcn/ui on Radix. Built output is served by the control plane; no Node at runtime. `pnpm check` runs the type check and `scripts/check-shadcn.mjs` (no legacy classes, no native elements or Radix imports outside `components/ui/`, no `@layer components`; no allowlist), and CI runs Playwright.
- `proto/` — protobuf schema for the config snapshot and the config-stream gRPC service. Single source of truth for types on both sides.
- `bench/` — mock upstream (fixed latency, SSE streaming) and load scripts comparing Lumewarp vs LiteLLM.
- `deploy/` — Helm chart, Prometheus rules, Grafana dashboards.

## Non-negotiable rules

1. **Hot path stays fast.** The gateway request path never touches a database, never calls the control plane, and never runs Python. It reads only the in-memory config snapshot. Counters and usage events are emitted asynchronously.
2. **Python only in the control plane.** Customization of auth providers is a Python plugin registered via entry points (`lumewarp.auth`, `lumewarp.authz`). Plugins produce declarative output compiled into the snapshot. Router strategies are Rust crates compiled into the gateway (`docs/router-strategies.md`), declaring their settings as schemas the control plane and UI read. Per-request hooks exist only as an opt-in external gRPC call with a timeout and a default route.
3. **Providers: OpenAI-compatible only.** Upstreams are vLLM and OpenAI (plus any OpenAI-compatible endpoint). Do not add translation layers for other providers. Pass through unknown request fields (e.g. vLLM `extra_body`, guided decoding) untouched.
4. **Config is an immutable versioned snapshot** pushed over a gRPC stream (full, then deltas; ACK/NACK). Gateway swaps atomically and persists the last good snapshot to disk so it can boot without the control plane. Gateway also supports a standalone YAML mode with the same schema.
5. **Observability is a feature, not an afterthought.** Every new gateway path gets Prometheus metrics (`lumewarp_*`, see the table in `docs/architecture.md`) and OTel spans. No API-key-level labels on metrics. Never log prompts/responses by default.
6. **Performance claims need numbers.** Any change on the gateway request path must run the `bench/` suite; don't regress p99 overhead.

## Current state

The Rust gateway with its bench against LiteLLM, the control plane, the gRPC config stream, usage and budgets, the Admin UI, identity, routing, the model catalog, self-service keys and credential storage are implemented. `docs/architecture.md` lists what exists and what is deferred.

Identity, routing and observability:

- Tenancy: org → teams → members (admin/member) and service accounts → API keys. Orgs and org admins exist (`orgs`, `org_admins`, `teams.org_id`) and org administration is inherited by their teams in `RequestAuthz` only; the authz backend answers direct relations and `member` stays direct membership. The org API exists: installation admins create, rename/set policy, archive and assign org admins; an org admin reads its own orgs and their per-team usage, creates and archives teams in them, and reads their audit (org-level events included). `POST /teams` takes an explicit `org_id`; there is no default org. `DELETE /teams/{id}` archives: one transaction revokes every key (rotation-grace hash included), deactivates the team's service accounts, frees the name (unique only among live teams) and the compiler skips the team; org archiving needs no live team and blocks new ones. Service accounts also have a deactivated state the compiler respects. Installation admins are rows in `installation_admins` (`proxy_admin` unique, or `admin`); the first verified login matching `LUMEWARP_INITIAL_ADMIN_EMAIL` becomes `proxy_admin` while none exists, a break-glass password opens an emergency session, and a break-glass session or the proxy_admin can transfer the role. The `proxy_admin` is the highest role and its account is protected from other installation admins.
- Admin login via OIDC (cookie sessions + CSRF); the IdP authenticates only and no claim grants a role. Authorization is pluggable (`builtin`, `openfga`, `http`, or a `lumewarp.authz` entry point) behind `check(user_id, relation, resource)`, where a resource is an org or a team and every backend (built-ins included) loads through the `lumewarp.authz` entry point; installation roles are read from Postgres and never asked of the backend, and backend errors are never denies. A key reconciler suspends keys of members removed in an external IAM.
- Gateway routing: retries (before any response byte, budgeted), fallbacks, circuit breaker, active health checks, LOWEST_LATENCY, VLLM_LOAD, traffic split with sticky hashing — each gated by a snapshot capability the control plane emits only when used.
- Observability: Prometheus alert rules (promtool-tested), Grafana dashboards, control-plane `/metrics` on its own listener.

The model catalog (`docs/architecture.md`, "模型目錄已實作"):

- Names: a client calls a model, an alias or a router, one namespace, all renameable, referenced by id. The response `model` is the called name, without buffering.
- Grants by id, checked against the called name. Rollback restores the catalog by id.
- Usage records the served model plus alias, router, fallback and attempts. No access log: a request is looked up in OTel by `x-lumewarp-request-id`.
- Strategies are Rust crates (`register!`), reported in `Hello` with their schemas, validated on write.

Self-service keys and credentials (`docs/architecture.md`, "Admin UI", "憑證儲存與稽核"):

- Self-service keys: any IdP user signs in just in time with no grants and manages their own keys in the teams they belong to; a team sets `max_keys_per_member` and `max_member_key_lifetime_seconds`, and `GET /users/me` returns effective models, limits, key limits and the month's remaining budget. A member resumes their own `owner` and `owner_removed` suspensions; `admin` and `owner_deactivated` need an administrator.
- Rotation grace: `grace_seconds` defaults to 0 and is at most 24 hours, with one previous hash; both secrets authenticate as the same key and usage stays on it, and the gateway stops accepting the old one at its own expiry from the snapshot alone. Key create, rotate and limits-only update are limited to 30 attempts per 60 s per principal and operation, per process, and a 429 is not audited.
- Admin UI: `/my-keys` is a plain member's home, a team's Keys tab manages its keys, access states are not denials, and one dialog above the router shows every one-time secret.
- Credentials at rest: only sha256 hashes and environment references, the OIDC nonce/PKCE in an AES-GCM cookie, `api_key_ref`, `metrics_url` without a query, and an allowlist for audit details.

Acceptance suites: `scripts/e2e-core.sh`, `scripts/e2e-identity.sh` (needs `scripts/dev-idp.sh up`), `scripts/load-e2e.sh`, `ui/` Playwright (`pnpm test:e2e`), and `bench/` A6 for request-path changes. The e2e suite scans the database for plaintext credentials.

Next: MCP / A2A, not planned yet.

Decided: license Apache-2.0; MVP usage data in Postgres (ClickHouse later).

