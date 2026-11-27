# Usage and audit

Usage aggregates gateway requests; [Requests](./requests.md) lists them one by one, and the
audit log records administrative changes. Usage and Requests come from the same recorded
usage events; audit answers a different question.

## Usage

Every request a gateway finishes becomes one usage event, stamped with the zone of its API
key and reported to the control plane about once a second. The Usage page and
`GET /api/v1/usage` aggregate them by zone, team, owner, key, called name, endpoint, model
type and status. The Admin UI header selects one zone; choose **All zones** to include every zone.
The API's optional `zone` filter behaves the same way: omitted means all zones. `zone` is
also a supported `group_by` dimension.

`GET /api/v1/usage` takes an optional `tz` query parameter, an IANA time zone name such as
`Asia/Taipei` (default `UTC`; an unknown name is 422). The `day` dimension is the calendar
day in that zone, and each row's `date` is the corresponding local `YYYY-MM-DD`. The `hour`
dimension buckets requests by the local clock hour in `tz` and returns the bucket start as
the row's `hour`, an ISO 8601 datetime with a UTC offset (for example
`2026-03-10T09:30:00Z`); a half-hour zone therefore starts an hour bucket on a `:30` UTC
instant, and the two 01:00 hours of a daylight-saving fall-back stay two buckets. A request
that groups by `hour` may span at most 31 days, otherwise it is 422. `compare=previous` keeps
working with either dimension. The monthly team-totals fast path is used only for `tz=UTC`.

Only an installation admin sees where a request was served: the `model`, `upstream_model`,
`alias` and `router` dimensions, their ids and `_missing` filters, and the `served_by`,
`via_aliases`, `via_routers` and `upstream_model(s)` provenance. Any other caller gets
`403` with the error code `routing_detail_forbidden` for those dimensions or filters, and
the provenance fields are null or empty; `fallback` is routing detail too. Everyone else
groups by the name the client called (`called`, or `requested` as typed) together with `day`,
`team`, `owner`, `key`, `endpoint`, `model type`, `status_class`, `outcome`, `kind`,
`target`, `method` and `tool`.

**The called name is the default grouping for everyone**; the Admin UI labels it
**Model**, whatever kind of name the client called. The `called` dimension is the catalog
item the client called (an alias, else a router, else a model; a name that matched nothing
stays the literal text), labelled with its current name and a `called_kind` of `model`,
`alias`, `router` or `unknown`. A rename therefore merges into one row, and `called_names`
lists every spelling clients used. `requested` stays the exact text as typed, and `model`
is the model that served, so a request answered by a fallback is attributed to the model
that served it. The `called` filter selects the same item.

Visibility follows the caller's role:

- A plain member sees the **whole team's totals** when the query neither groups nor filters
  by owner or key: the team's requests by called name, day, endpoint and so on are summed
  over every key in the team, service accounts included.
- Grouping by `owner` or `key`, or filtering by `user_id`, `service_account_id` or `key_id`,
  falls back to the narrower rule: in a team the member does not administer they see only
  their own user-owned keys. Other members' keys and owners are never split out for a
  plain member, whatever they ask for. In a team of two, the team total minus the member's
  own still tells them the other member's total; no name or id is shown.
- A team admin (including an org admin by inheritance) and an installation admin keep the
  whole-team view, including owner and key grouping and filtering.
- An installation admin sees every team as well.

Grouped by `requested`, a renamed model, alias or router appears as separate rows before
and after the rename, because usage stores the name the client sent; `called` merges them.
The `requested` filter selects one exact spelling.

![The Usage page, by model](/screenshots/usage.png)

**From** and **To** are date and time pickers in the browser's time zone, and every request
the page makes sends that zone as `tz`. **To** includes the chosen minute. The defaults are
local midnight 29 days ago and now. The chart and the insight sparklines bucket by local
hour when the range is at most 48 hours, and by local day otherwise.

The filter bar matches the one on Requests: range, zone, team, model and **Group by** on one
row on a wide screen, and upstream model, alias, router, endpoint and model type under **More filters**. The
MCP and A2A tabs have the range, zone, team and **Group by** only. The routing-specific
filters and group-by choices are only available to an installation admin.

**Group by** offers Model (the called name, the default), Served model, Model × served,
As typed, Upstream model, Alias, Router, Endpoint, Model type, Status, Team, Owner, API key
and Day; a member gets the ones that are not routing detail. With **All zones** each row also
names its zone.

The LLM tab has a **View** switch beside the tabs, **Usage** or **Cost** (kept per browser,
Usage by default), that reshapes the whole tab.

- **Usage** shows an insights row of requests, tokens, error rate, p95 latency and
  (installation admin only) fallback rate for the range, each with its change against the
  previous range of the same length (`compare=previous`), shown as an arrow and a percentage
(percentage points for a rate), and a sparkline of the metric over the range behind it. p95
latency and fallback rate have no sparkline with **All zones**, as a percentile cannot be
merged across zones. The chart's metric is tokens, requests or errors, the
  leaderboards rank by tokens, and the table lists requests, tokens (with a bar), prompt and
  completion tokens, audio seconds, error share and p95.
- **Cost** shows spend, average cost per request, cost per million tokens and the number of
  unpriced requests, with the same change against the previous range (a rise in spend is not
  coloured as bad). The chart draws cost only, the leaderboards rank by spend, and the table
  lists requests, cost (with a bar), each row's share of the table's total cost, cost per
  request and unpriced requests. The note about requests counted at $0 for lack of a price
  appears here.

The **Top 10** card follows the same view: a switch picks **Models**, **Keys** or **Users**,
and each row lists tokens and requests in aligned columns (spend and requests in the Cost
view) with a bar of the ranked value. Without a zone filter a name, key or user is one row
across zones. Only an installation admin sees whether a model row is a model, an alias or a
router. For a plain member, **Keys** and **Users** are offered only when one of the rows is
their own; otherwise they would repeat the total under an anonymous label.

![The Cost view](/screenshots/usage-cost.png)

**The chart follows the grouping.** Its series are the first non-day dimension of
**Group by** (`group_by=day,<dimension>` or `hour,<dimension>` with the page's filters;
**Day** is a single series). Click a legend entry to hide or show its series; **Show all**
brings every series back. The top eight series by the chosen metric keep a palette colour and the rest fold
into **Other**, whose names the tooltip and legend list. Except when grouped by **Day**, the chart type switches between
stacked bars over local days or hours, a share donut and ranked bars, and, on the Usage view, the
metric between tokens, requests and errors (kept per browser). A model with no price is named and left out of
cost instead of being drawn as zero; **Show data table** lists the same numbers as text.

On the Usage view the table adds the error share and p95 latency per row (p95 is empty where the answer came
from the monthly totals, which hold no durations). A model row shows, to an installation
admin, whether it is a model, an alias or a router and, with several spellings, lists them in a tooltip; an installation admin can open **Served
by** on it for the model × served breakdown (served model, upstream model, fallback share,
p95). **View** opens [Requests](./requests.md) on the same name, range (to the minute) and team. The MCP
and A2A tabs add a status donut and ranked bars of the top servers, agents, methods or
tools by requests or errors.

Grouped by **API key**, each key is a pill; hover or focus it for its org, team, owner or
service account (when known), prefix and id. `GET /api/v1/usage` rows grouped by `key` carry
`key` in the same shape as the request log's `key`.

An event records the name the client called, the model that served it, the alias, router and
fallback it went through, the number of attempts, tokens, audio duration and cost. Only an
installation admin sees the served model and routing chain; every other caller sees the
called name. Prompts and responses are never recorded. The same events back the
[request log](./requests.md), which lists one row per request with the full name-resolution
chain; to look at one request's per-attempt detail, use its `x-lumewarp-request-id` in your
tracing backend ([Metrics, alerts and tracing](../operations/observability.md)).

## Leaderboard

`GET /api/v1/usage/leaderboard` (`getUsageLeaderboard`) ranks the caller's readable teams by
tokens or by spend. `zone` optionally restricts the results; omitted means every zone and
merges the zones: the same called name or owner is one row whose `zone` is null, with its
tokens summed across zones. Passing `zone` keeps the board per zone, and each row then
names that zone. Installation admins rank every team; everyone else only the teams they
can read. `by` selects the board:

| `by` | Ranks | Extra row fields |
| --- | --- | --- |
| `called` | The catalog items clients called, renames merged, summed across the readable teams (and, without a `zone` filter, every zone) | `zone` (null when merged), `called`, `called_kind` |
| `requested` | The names as typed, summed across the readable teams (and, without a `zone` filter, every zone) | `zone` (null when merged), `requested` |
| `key` | API keys, grouped within their team (and, with a `zone` filter, within that zone) | `zone` (null when merged), `team_id`, `key` (when readable) |
| `owner` | Users and service accounts, grouped within their team (and, with a `zone` filter, within that zone) | `zone` (null when merged), `team_id`, `owner_kind`, `owner_id`, `is_me` |

Rows are ordered by `total_tokens` descending, then `cost_nano_usd`, or with `order=cost` by
`cost_nano_usd` descending, then `total_tokens` (the Usage page's **Cost** view), and numbered
from `rank=1`. The other query parameters are the range (`from`, `to`, defaulting to the
current UTC month), `team_id`, `zone`, `kind` (`llm` by default), `limit` (default 10,
maximum 50) and `order` (`tokens` by default, or `cost`). Each row also carries `requests`, `prompt_tokens`, `completion_tokens`, `total_tokens`,
`cost_nano_usd` and `unknown_cost_count`.

An identity is named only when the caller may read it. For `by=key`, `key` is a
[key reference](./requests.md#the-api) for an installation admin, the key's team admin, or
the key's user owner; otherwise it is null and `label` is `Key #<rank>`. For `by=owner`,
`owner_kind` and `owner_id` are filled for an installation admin, that team's admin, or the
row's own owner; otherwise both are null and `label` is `Member #<rank>`. In both cases the
unreadable key's or owner's id stays hidden, and `label` names the readable row. `is_me` is
true only for the caller's own owner row, which is always named.

Org usage can also be filtered by zone (`GET /api/v1/orgs/{id}/usage?zone=...`); without a
zone it summarizes every zone.

## Budget enforcement

Rate limits and budgets are per zone, even though the org, team and key-owner hierarchy is
shared. Usage totals and budget windows are kept separately for each zone. Each newly
reported LLM event adds cost once to the zone's team total and once to its owner total; org
spend is derived from that zone's teams. When a budget is exhausted, the affected keys in
that zone are recompiled to `BUDGET_EXCEEDED`. Enforcement is eventual: in-flight streams
are not cut, and unreported events can be lost if a gateway's bounded queue fills or it
crashes. MCP and A2A events are recorded but have no budgets or token/rate limits of their
own.

Usage is stored in Postgres; ClickHouse is later. While the control plane is down, the
gateway keeps serving from its snapshot, but queued usage events are in memory only.

## Audit

The audit log records administrative changes only. A model request, including one sent from
the Playground, is not an audit entry; it appears in [Requests](./requests.md) instead. The
page filters by range (empty means no bound; the end day is inclusive), org, team and action.
Every management mutation writes an `audit_log` row in the same transaction. Reads are
installation-wide for installation admins; team admins see rows of the teams they administer
and org admins additionally see their org-level events and rows for archived teams. A plain
member has no audit view (`403`). Audit is not filtered by the header zone: some events
change global identity, grants or catalog definitions, and zone-specific events name their
zone in the event details.

Every row also records `via`, how the caller authenticated: `session` for a browser session
(break-glass included), `pat` for a personal access token and `mcp` for a tool call through
the control plane's [MCP server](../self-service/mcp-client.md). It is null for gateway and
system events and for rows written before the field existed. `GET /api/v1/audit` takes an
optional `via=` filter with those three values; the Audit page shows it as a badge in the
**Via** column and filters on it.

`details` is an allowlist per action (`DETAIL_ALLOWLIST` in `audit.py`): only fields
classified for that action are stored, and anything else is dropped rather than redacted.
Nested objects such as `changes`, `settings` and `traffic_split` have their own field lists,
and free-form strategy params are never recorded. The failure column
(`config_outbox.last_error`) keeps only the exception class name. A test scans every
`audit.record(action=...)` call site, so a new action without a classification fails the
suite, and `tests/test_schema_secrets.py` fails when a new Text/String/JSONB column is not
classified as non-secret.

![The audit log](/screenshots/audit.png)

A key resource is shown as the same key pill; other resource types keep their type and a
short id. `GET /api/v1/audit` entries with `resource_type: "key"` carry `key`.
