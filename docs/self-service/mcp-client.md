# Use Lumewarp from an MCP client

The control plane answers MCP at `/mcp`, so an MCP client can work with Lumewarp itself the
way the Admin UI and the REST API do: read what you can call, your limits and budget, manage
your own API keys, and read usage, requests and incident status. Every tool runs the same
REST operation, with the same permissions, validation, rate limits and audit.

This is not the gateway's `/mcp`. The gateway endpoint ([MCP](../using/mcp.md)) proxies the
upstream MCP servers an administrator registered and is called with an `lw-` key; it reaches
models and third-party tools. The control plane endpoint manages Lumewarp and is called with
a personal access token (`lwp-…`).

Administrators get read-only tools for tenancy and incidents. Every administrative change
stays in the Admin UI or the REST API.

## Connect

Create a personal access token under **Personal access tokens** on **My keys**
([Your keys](./index.md#personal-access-tokens)). It acts as you with your roles, so treat it
like a password. The endpoint is your control plane's public URL followed by `/mcp`, for
example `https://lumewarp.example.com/mcp`.

### Claude Code

```sh
claude mcp add --transport http lumewarp https://lumewarp.example.com/mcp \
  --header "Authorization: Bearer lwp-..."
```

### Cursor

In `.cursor/mcp.json` for one project, or `~/.cursor/mcp.json` for every project:

```json
{
  "mcpServers": {
    "lumewarp": {
      "url": "https://lumewarp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer lwp-..."
      }
    }
  }
}
```

### VS Code

In `.vscode/mcp.json`:

```json
{
  "servers": {
    "lumewarp": {
      "type": "http",
      "url": "https://lumewarp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer lwp-..."
      }
    }
  }
}
```

### claude.ai

Add a custom connector with the same URL and a static bearer token as a request header. An
organization owner enters the token when adding the connector (Claude's static-credential
option is in beta). Do not configure OAuth: Lumewarp does not offer it yet.

Any other MCP client needs Streamable HTTP, the URL and the `Authorization` header. The
server keeps no session: it sends no `Mcp-Session-Id`, so no session affinity is needed.

Lumewarp checks the `Origin` of browser clients against
`LUMEWARP_API_MCP_ALLOWED_ORIGINS`; a request without an `Origin` header (every
command-line or editor client) is always accepted. See [Control plane
settings](../operations/control-plane.md).

## Tools

The server exposes 27 tools in five toolsets. `tools/list` returns the tools your roles
allow; a tool you cannot use is not listed, and calling it anyway is answered by the REST
API's own refusal.

| Toolset | Tools | Minimum role |
| --- | --- | --- |
| `me` | `get_current_user`, `list_zones`, `list_catalog`, `list_teams`, `get_team` | member |
| `keys` | `list_own_keys`, `get_key`, `create_my_key`, `update_my_key`, `revoke_my_key`, `rotate_my_key` | member |
| `usage` | `get_usage`, `get_usage_leaderboard`, `list_requests` | member |
| `incidents` | `get_status` | member |
| `incidents` | `list_incidents`, `get_incident` | installation admin |
| `tenancy` | `list_team_keys`, `list_members`, `list_service_accounts`, `get_service_account` | team admin |
| `tenancy` | `list_orgs`, `get_org`, `get_org_usage` | org admin |
| `tenancy` | `list_org_admins`, `list_users`, `get_user` | installation admin |

A member is listed 15 tools, a team admin 19, an org admin 22 and an installation admin 27.
Roles are hierarchical: a team admin also sees the member tools, and an org or installation
admin sees everything below it.

- **`me`** reads what you can call. `get_current_user` returns your effective models, limits,
  key limits and this month's remaining budget by zone; `list_zones`, `list_catalog`,
  `list_teams` and `get_team` read the zones, names, teams and one team's basic record that
  the Admin UI shows.
- **`keys`** is the only toolset that writes. `list_own_keys` and `get_key` read keys you may
  read; `create_my_key`, `update_my_key`, `revoke_my_key` and `rotate_my_key` change only your
  own keys (see below).
- **`usage`** reads the same numbers as the Usage and Requests pages, with the same
  visibility. `get_usage` and `get_usage_leaderboard` follow the Usage page's scope; a plain
  member's `list_requests` sees requests made with their own user-owned keys, while a team
  admin sees the teams they administer.
- **`incidents`**: `get_status` lists the firing, user-visible alerts on names you can call,
  the same as the status banner. An installation admin also gets `list_incidents` and
  `get_incident` (history, member alerts, affected names and traffic), as on
  [Incidents](../admin/incidents.md).
- **`tenancy`** is read-only. A team admin reads the team's keys, members and service
  accounts; an org admin also reads its orgs and their usage; an installation admin also
  reads org admins and users. No tool creates or changes an org, team, member, service
  account, upstream, model, alias, router, gateway, zone or grant, and no tool mints a
  personal access token.

### Narrow the list

Pass `?toolsets=` on the URL or send `X-Lumewarp-MCP-Toolsets` with a comma-separated subset
of `me,keys,usage,incidents,tenancy`:

```
https://lumewarp.example.com/mcp?toolsets=me,usage
```

Without either, every toolset your role allows is listed. An unknown toolset name is 400; a
call to a tool outside the selected toolsets is `unknown_tool`. The role filter only affects
the list: a role-hidden tool called anyway reaches REST and is refused there.

## Your own keys

`create_my_key` creates a user-owned key in a team where you may create one, under the
zone's key rules, the same as **Create** on My keys. It has no service-account owner: the
argument is rejected.

`update_my_key`, `revoke_my_key` and `rotate_my_key` first read the key and answer
`not_your_key` unless it is user-owned and the owner is you. A team admin cannot rename,
revoke or rotate a colleague's key or a service-account key here, although the REST API would
allow it; use the team's **Keys** tab or REST for those. `update_my_key` changes the name,
expiry, limits, model access or lifecycle (suspend and resume), within the same rules as
REST.

`revoke_my_key` and `rotate_my_key` are destructive and require `confirm: true`, a JSON
boolean and not the string `"true"`. Without it they change nothing and answer
`confirmation_required` with the effect spelled out. `rotate_my_key` takes `grace_seconds`
(0 by default, at most 24 hours), as REST does.

The same rate limits apply as on the API: creating, updating and rotating keys is limited to
30 attempts per minute per principal and operation, and a 429 comes back as a tool error.

## Secrets

`create_my_key` and `rotate_my_key` mask the new secret in the result as
`<prefix>… (masked)` and add a note. Lumewarp shows a secret once; a masked result means the
key is usable only after you rotate it again with `reveal_secret: true`, or after you create
the key on **My keys** and copy the secret there.

Pass `reveal_secret: true` (a JSON boolean) to get the plaintext. The result then starts with
a warning: the secret is in the conversation and possibly in the client's logs. Store it in a
secret manager, and rotate the key if the conversation is shared. `reveal_secret` is only
defined on `create_my_key` and `rotate_my_key`; another tool rejects it as an unknown
argument.

## Errors

A REST failure comes back as a tool error: `isError: true`, and a text block with
`{"status": 403, "error": {"code": ..., "message": ...}}`. The statuses and codes are the REST
API's (`forbidden`, `not_found`, `validation_error`, `routing_detail_forbidden` and so on);
MCP adds `confirmation_required`, `not_your_key`, `invalid_arguments` and `unknown_tool`. A
refused call changes nothing.

At the HTTP level, `/mcp` itself answers:

- 401 without a valid personal access token, with `WWW-Authenticate: Bearer
  resource_metadata="<control plane>/.well-known/oauth-protected-resource/mcp"`. The metadata
  document describes `/mcp` and says the bearer token goes in the `Authorization` header.
  Lumewarp does not implement OAuth yet, so a client that follows the challenge finds no
  authorization server: configure the token header instead.
- 403 for a browser `Origin` that is not allowed, 400 for an unknown toolset, 413 for a body
  over 4 MiB and 404 when `LUMEWARP_API_MCP_ENABLED=false`.
- 405 for any method but `POST`: the endpoint is stateless and answers every request with one
  JSON body, so there is no `GET` event stream and no session to `DELETE`.

Every `/mcp` response carries `Cache-Control: no-store`. Set `LUMEWARP_PUBLIC_URL` on the
control plane when `/mcp` is reachable: without it the 401 challenge and the metadata document
are built from the request's `Host` header, and the control plane logs a warning at startup.

Session cookies and gateway keys (`lw-…`) are refused with 401: this endpoint takes a
personal access token only.

## Audit

A tool call runs the same REST handler as the matching request, so a write is audited exactly
as it would be from the Admin UI, with the same details. `via` records how the caller
authenticated: `mcp` for these calls, `pat` for a direct REST call and `session` for the
browser. `GET /api/v1/audit?via=mcp` lists only the MCP ones. [Usage and
audit](../admin/usage-and-audit.md#audit) describes the audit scope.
