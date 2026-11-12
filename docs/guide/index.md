# What Lumewarp is

Lumewarp is an LLM gateway for OpenAI-compatible upstreams such as vLLM and OpenAI. Clients
send ordinary OpenAI API requests to one address; Lumewarp authenticates them, enforces
limits and budgets, picks an upstream and records usage.

It is two programs:

- **The gateway** (Rust) is the data plane: the address clients call. On the request path
  it reads only an in-memory copy of the configuration. It never queries a database and
  never calls the control plane, so the control plane can be down while the gateway keeps
  serving.
- **The control plane** (Python, with Postgres) is where people work: the Admin UI and the
  management API for teams, keys, models and routers. It compiles every change into a
  versioned snapshot and pushes it to the gateways, and it collects their usage reports.

A gateway can also run alone, from one YAML file, with no control plane and no database.

## What it does

- **OpenAI-compatible proxying** for chat completions, completions, responses, embeddings,
  audio transcription and decision endpoints. Unknown request fields pass through
  untouched, so vLLM extensions such as guided decoding keep working.
- **Names instead of upstreams.** Clients call a model, an alias or a router. A router adds
  a strategy (weighted, least in flight, lowest latency, vLLM load, or your own plugin),
  retries, fallbacks, circuit breaking and traffic splits.
- **Tenancy and self-service.** Orgs contain teams, teams contain members and service
  accounts, and those own API keys. Anyone who signs in manages their own keys within the
  limits their team sets.
- **Limits and budgets** along org → team → owner → key: requests and tokens per minute,
  concurrency, and a monthly budget.
- **MCP servers and A2A agents** proxied on the same gateway, granted per key.
- **Observability**: Prometheus metrics, OpenTelemetry traces, alert rules and dashboards.
- **Extension points**: WASM plugins in the gateway, Python plugins in the control plane.

Upstreams are OpenAI-compatible only: Lumewarp does not translate to other providers' APIs.

## Where to go next

| You want to | Read |
| --- | --- |
| Understand the vocabulary | [Concepts](./concepts.md) |
| Try it on your machine | [Quick start: standalone gateway](./quickstart-standalone.md), then [with the control plane](./quickstart-control-plane.md) |
| Call models with a key you were given | [Calling the gateway](../using/index.md) |
| Create and rotate your own keys | [Your keys](../self-service/index.md) |
| Manage a team or an org | [Roles](../admin/index.md) |
| Install and run Lumewarp | [Deploy](../operations/index.md): [Helm](../operations/helm.md) or [Docker](../operations/docker.md) |
| Add a router strategy or an auth plugin | [Extension points](../extending/index.md) |
