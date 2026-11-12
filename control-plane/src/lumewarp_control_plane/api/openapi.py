"""Document-level OpenAPI metadata: the description, the tag descriptions and shared responses.

Operation descriptions are the handlers' docstrings and field descriptions live on the
schemas; `tests/api/test_openapi.py` keeps both complete.
"""

from typing import Literal

from pydantic import BaseModel, Field

from lumewarp_control_plane.api.schemas import ErrorBody

DESCRIPTION = """\
The management API of a Lumewarp installation: tenancy (orgs, teams, members, service
accounts), virtual API keys, the model catalog (upstreams, models, aliases, routers), MCP
servers and A2A agents, usage, audit, gateways and config versions. The Admin UI uses only
this API. Clients call models through the gateway, which has its own API document.

## Authentication

Send a personal access token as `Authorization: Bearer lwp-…`, or the browser session cookie.
A request that writes with the session cookie must also send `X-Lumewarp-CSRF`. Virtual API
keys (`lw-…`) and gateway tokens (`lwg-…`) are not accepted here.

## Authorization

Each operation states the role it needs. Roles are inherited downwards: an installation
admin administers every org, and an org admin every team of the org. A resource the caller
may not read answers 404, as if it did not exist; a resource the caller may read but not
change answers 403.

## Lists

A list returns `{items, next_cursor}`. Pass `next_cursor` back as `cursor` for the next page;
`null` means the last page. `limit` is 1 to 200 and defaults to 50.

## Errors

Every non-2xx response is `{"error": {"code": "…", "message": "…"}}`. `code` is stable and
meant for programs, `message` for people. A 422 names the offending field in its message.

## Secrets

A key, token or gateway token is returned once, by the operation that creates or rotates it,
and only its SHA-256 hash is stored. Those responses carry `Cache-Control: no-store`.

## Changes reach gateways asynchronously

A write that changes what gateways enforce is compiled into a new config version and pushed
to them, normally within a second. `GET /api/v1/gateways` shows the version each gateway has
acknowledged.
"""

TAGS = [
    {"name": "session", "description": "Browser login, logout and the current session."},
    {
        "name": "users",
        "description": "Users, the caller's own profile (`/users/me`) and personal access tokens.",
    },
    {
        "name": "installation-admins",
        "description": "The installation roles `proxy_admin` (one, the highest) and `admin`.",
    },
    {
        "name": "orgs",
        "description": "Orgs group teams and cap their policy. Managed by installation "
        "admins; an org admin reads their own orgs.",
    },
    {"name": "teams", "description": "Teams own members, service accounts and keys."},
    {
        "name": "members",
        "description": "Team memberships (`admin` or `member`) and each member's policy.",
    },
    {"name": "service-accounts", "description": "Non-human key owners of a team."},
    {
        "name": "keys",
        "description": "Virtual API keys (`lw-…`), the credential clients send to the gateway.",
    },
    {
        "name": "routing",
        "description": "The model catalog: upstreams, models, aliases and routers, which "
        "share one namespace of callable names; and the gateway plugins connected "
        "gateways report.",
    },
    {
        "name": "mcp",
        "description": "MCP servers and A2A agents proxied by the gateway, and the review of "
        "MCP tool and prompt definitions.",
    },
    {"name": "playground", "description": "Backs the Admin UI Playground. Session only."},
    {"name": "usage", "description": "Aggregated usage and cost."},
    {"name": "audit", "description": "The audit log of administrative changes."},
    {"name": "gateways", "description": "Connected gateways and their tokens (`lwg-…`)."},
    {"name": "config", "description": "Compiled config versions and rollback."},
    {"name": "health", "description": "Liveness and readiness. No authentication."},
]


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, str] = Field(
        description="One entry per check (database, migrations, background tasks): `ok`, "
        "or what is wrong."
    )


RATE_LIMITED: dict[int | str, dict] = {
    429: {
        "model": ErrorBody,
        "description": "Too many attempts; `Retry-After` gives the seconds to wait",
    }
}
