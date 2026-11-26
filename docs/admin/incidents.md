# Incidents

An **incident** is one outage as Alertmanager reported it: the alerts of one alert name, zone
and set of targets whose intervals overlap. The shipped rules fire once per gateway, so an
outage seen by four gateways is four alerts and one incident, "reported by 4 gateways".
Installation admins read the history here; every signed-in user sees the firing alerts as a
status banner, limited to the names they can call. Alerts come from your Prometheus rules;
how to connect Alertmanager and which labels make an alert visible to users are in [Alerts and
incidents](../operations/alerts-and-incidents.md). Lumewarp shows only what it received and its
own request log; it runs no Prometheus queries.

## The status banner

A banner under the header of every page shows the firing, user-visible alerts that touch a
name **you** may call, in a zone where it is deployed. A name appears once per zone even when
several alerts touch it (the worst state wins), and a model alert also warns the aliases and
routers that route to it.

- One affected name is one line: the state (`down`, `degraded`, `congested`, or whatever the
  alert's `lumewarp_status` says), the name, the zone when you have more than one, the alert's
  `lumewarp_summary` text and since when (the exact time in a tooltip).
- Several names fold behind the worst state (`down` before `congested` before `degraded`) and
  its text, with an "N names affected" button that expands the list: state, name, zone and
  since for each.

A user with no grant on the affected names sees no banner, and nobody sees upstream names or
labels. For an installation admin each name links to its incident. Nothing is shown while no
alert is firing, or when Alertmanager is not connected.

## The incident list

**Incidents** (installation admins only) has two parts:

- **Firing now**: every firing incident, whatever the time range, on top. Users who can call
  an affected name see the banner for these.
- **Resolved**: resolved incidents that started in the time range, newest first, paged.

Each row shows how long the incident has been firing or lasted (for a resolved incident, the
time to repair), the alert's `summary` (or its name), the alert name without the `Lumewarp`
prefix, its targets (the models, aliases, routers and upstreams its labels resolved to), "N
gateways" when several gateways reported it (counted by the alerts' `instance` label, else
`gateway_id`; the list in a tooltip) or "N alerts" when several alerts without either label
were grouped, what users saw (the `lumewarp_status`, or "Admin
only" when the alert never reached a banner), the zone ("Every zone" for an alert without one),
when it started and its severity.

The filters are the time range (24 hours, 7 days, 30 days, or a custom start range), the zone
(which also includes incidents without a zone), firing or resolved (which hides the other
part), the impact (user-visible or admin only) and a search over the alert name, the `summary`
and the target names. Incidents are kept forever. An alert whose names matched nothing in the
catalog is listed too, with no targets.

### How alerts are grouped

When an alert arrives for the first time it joins the incident with the same alert name, the
same zone (or both without one) and the same resolved targets whose interval overlaps its own
(the incident is still firing, or ended after the alert started); otherwise it opens a new
incident. An alert with no resolved target is grouped by its labels instead, ignoring
`instance`, `gateway_id`, `pod`, `job`, `endpoint` and `service`. The incident starts at its
earliest alert, fires while any alert fires, ends at the latest end once all resolved, and takes
the worst severity and state of its alerts. An alert never moves to another incident later,
and a new firing after the incident resolved is a new incident.

## An incident

Click a row to open it. The page shows:

- whether it is firing (and for how long) or resolved (and after how long), what users saw and
  the severity; the `summary` as the title and the `description`; the full alert name, zone,
  start, end, which gateways reported it and the targets. **View failed requests** jumps to the
  failed requests below, and **Open in Prometheus** follows the alert's link back to the
  expression that fired, when Alertmanager sent an `http` or `https` link;
- **What users saw**, for a user-visible incident: the banner text and every name the targets
  reach, by zone, whoever may call it;
- a **timeline** of each gateway's alert and of the **related** incidents that share a target
  within an hour, on one time axis with the incident window shaded, so a cascade reads as one
  story;
- the **traffic** Lumewarp saw around it, from the request log, restricted to the affected
  names (or the models on the affected upstream) and the incident's zone, from 10 minutes
  before it started until it resolved or now: the number of requests, how many failed and the
  most common error code, how many of the requests sent to a fallback succeeded without an
  error code (the same requests the chart shows as via fallback), and the median attempts of
  the requests that reached an upstream; a per-minute chart with the incident window shaded, switchable
  between requests (succeeded, via fallback and failed, stacked), time to first token (average
  and p95 of streamed requests) and context (average and p95 prompt tokens of requests that
  reported usage); the error codes, the top ten teams and the top ten keys with their requests
  and failures, and the 50 most recent failed requests (a status of 400 or above, an error
  code, or a stream cut after a 200 header) with the called and served name, the status, the
  error code, the team and a link to each in [Requests](./requests.md). A window longer than
  24 hours is cut to the newest 24 hours, and the page says so. It holds metadata only; no
  prompt or response.

Traffic is attributed by the called name, the served model, the alias or router, and the
upstream name, so a router incident includes traffic that was routed through it.

## The API

All of it requires an installation admin, except `GET /status`.

| Operation | |
| --- | --- |
| `GET /api/v1/status` (`getStatus`) | Any signed-in user: `{items: [{name, zone, state, summary, since}]}`; installation admins also get `incident_id`. |
| `GET /api/v1/incidents` (`listIncidents`) | `zone`, `status` (`firing` or `resolved`), `impact` (`user_visible` or `admin_only`), `q`, `from`, `to`, `limit`, `cursor`. Each row carries `alert_count` and `gateways`. |
| `GET /api/v1/incidents/{id}` (`getIncident`) | The row, its `alerts`, the `affected` names, `related` incidents and `traffic`. |

The receiver, `POST /api/v1/alerts/alertmanager` (`receiveAlertmanagerWebhook`), is called by
Alertmanager with its own bearer token, not by people; see [Alerts and
incidents](../operations/alerts-and-incidents.md#turn-the-receiver-on).
