# Concepts

## Gateway, control plane, snapshot

Clients call the **gateway**. People configure Lumewarp in the **control plane**. Between
them travels the **snapshot**: one immutable, versioned copy of everything a gateway needs
to enforce (keys as hashes, effective limits, models, routers).

A change in the control plane becomes a new config version, normally within a second. Each
gateway applies it, writes it to disk and acknowledges it. A gateway that restarts while
the control plane is down boots from the version on its disk.

Two consequences are worth knowing:

- **Changes are eventually enforced.** A revoked key stops working when the gateways have
  applied the next version, not at the instant you click.
- **Budgets are eventually enforced too.** Spend is added up from usage reports, so a key
  may overshoot its budget by what it spends in the few seconds before the gateways learn
  that it is exhausted.

## Names: models, aliases and routers

A client puts a **name** in the `model` field. A name is one of three things, and all
three share one namespace:

| Kind | What it is |
| --- | --- |
| **Model** | One model of one upstream, with its price and model type. |
| **Alias** | Another name for a model or a router. |
| **Router** | A strategy over several models, with retries, fallbacks and traffic splits. |

An **upstream** is an OpenAI-compatible endpoint: its base URL and how to authenticate to
it. Models belong to upstreams; clients never name an upstream.

Every name has a **model type** that decides which endpoints may call it: `llm`,
`embedding`, `transcription` or `decision`.

Names can be renamed without breaking anything, because grants, aliases and routers refer
to them by id. The response's `model` field is always the name the client called, whatever
served it; usage records both.

## Tenancy

```
installation
└── org                  caps what its teams may use
    └── team             owns members, service accounts and keys
        ├── member       a person; owns their own keys
        ├── service account   a non-human key owner
        └── API key      the credential a client sends
```

- An **org** groups teams and sets the outer limits for all of them.
- A **team** is where work happens: its policy says which names its keys may call, how
  fast, and within what budget.
- A **member** is a user with role `admin` or `member` in a team.
- A **service account** owns keys for an application, so they do not disappear when a
  person leaves.
- An **API key** (`lw-…`) belongs to one team and one owner.

## Policy, limits and budgets

Each level can narrow what the level above allows:

- **Models**: the names a key may call are the intersection down the chain.
- **Limits**: requests per minute, tokens per minute and concurrent requests; the lowest
  value along the chain wins.
- **Budget**: nano-USD per UTC calendar month, checked at the org, the team and the owner.

A key's **effective policy** is the result of the whole chain. Members see their own
effective policy; they do not see the team's full policy.

Rate limits are counted in each gateway process unless the gateways share a Redis and
namespace (`--shared-limits-redis-url`); then one window is shared and the instances may
over-admit by about their count, plus up to five sync intervals of traffic when an instance first
joins the scope. A gateway that loses Redis falls back to counting for itself, which is
the N-times behaviour again.

## Credentials

| Credential | Looks like | Used for |
| --- | --- | --- |
| API key | `lw-…` | Clients calling the gateway. |
| Personal access token | `lwp-…` | A person or script calling the management API. |
| Gateway token | `lwg-…` | A gateway connecting to the control plane. |
| Session | cookie | The Admin UI after signing in. |

Each secret is shown once, when it is created or rotated. Only its SHA-256 hash is stored.

## Roles

`proxy_admin` → `admin` → org admin → team admin → member, each inheriting the
administration of the level below. See [Roles](../admin/index.md).
