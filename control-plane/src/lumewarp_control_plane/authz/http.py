"""Authorization delegated to an HTTP service.

The request shape is the same for every resource:

    POST <authz_http_url>
    {
      "user_id": "<lumewarp user uuid>",
      "relation": "admin" | "member",
      "resource": {"type": "org" | "team", "id": "<uuid>"}
    }

The answer must be `{"decision": "allow" | "deny"}`. Anything else (a non-2xx status, a
timeout, an unknown decision) is `Decision.ERROR`, which the API answers with 503: it is
never a deny. The service answers direct relations only; `member` means a direct team
membership and must not be granted to an org admin by inheritance (Lumewarp applies that
itself, from `team.org_id`).

Two optional endpoints make the backend faster; each is used only when its URL is set
(`authz_http_list_url`, `authz_http_changes_url`), and the backend has no such method
otherwise, so Lumewarp falls back to one `check` per candidate and to periodic rescans.

    POST <authz_http_list_url>
    {"user_id": "<uuid>", "relation": "admin" | "member", "resource_type": "org" | "team"}
    -> {"ids": ["<id>", ...]}

The ids are the objects where the user holds the direct relation (same rule as `check`: no
org-to-team inheritance; a team admin is also a member).

    POST <authz_http_changes_url>
    {"cursor": "<opaque string>" | null}
    -> {"user_ids": ["<uuid>", ...], "cursor": "<non-empty string>", "reset": false}

`user_ids` are the users whose relations may have changed since `cursor`; the returned
cursor goes into the next call. A null cursor is the first call: Lumewarp then rescans
everything, whatever the service answers. `reset: true` asks for a full rescan (cursor
unknown or expired). Both endpoints use the same bearer token and timeout as `check`; a
non-2xx status or an invalid body is an error (503 on the API), never an empty answer.

Lumewarp never writes to the service (`manages_grants = False`), so the member endpoints of
the admin API are read-only under this backend.
"""

from typing import Any, Literal

import httpx

from lumewarp_control_plane.authz import (
    AuthzContext,
    ChangeBatch,
    Decision,
    Relation,
    Resource,
)
from lumewarp_control_plane.settings import Settings


class HttpBackend:
    manages_grants = False

    def __init__(self, settings: Settings) -> None:
        if not settings.authz_http_url:
            raise ValueError("http authorization requires authz_http_url")
        self._url = settings.authz_http_url
        headers = {}
        if settings.authz_http_token is not None:
            headers["Authorization"] = f"Bearer {settings.authz_http_token.get_secret_value()}"
        self._client = httpx.AsyncClient(headers=headers, timeout=settings.authz_timeout_seconds)
        # Optional capabilities are instance attributes set only when configured: the
        # Authorizer detects them with getattr, so an unset URL must leave no attribute.
        if settings.authz_http_list_url:
            self._list_url = settings.authz_http_list_url
            self.list_objects = self._list_objects
        if settings.authz_http_changes_url:
            self._changes_url = settings.authz_http_changes_url
            self.changes = self._changes

    async def check(self, user_id: str, relation: Relation, resource: Resource) -> Decision:
        response = await self._client.post(
            self._url,
            json={
                "user_id": user_id,
                "relation": relation,
                "resource": {"type": resource.type, "id": resource.id},
            },
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("decision") not in ("allow", "deny"):
            raise ValueError("invalid HTTP authorization decision")
        return Decision(body["decision"])

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("invalid HTTP authorization response")
        return body

    async def _list_objects(
        self, user_id: str, relation: Relation, resource_type: Literal["org", "team"]
    ) -> list[str]:
        body = await self._post_json(
            self._list_url,
            {"user_id": user_id, "relation": relation, "resource_type": resource_type},
        )
        ids = body.get("ids")
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise ValueError("invalid HTTP authorization ids")
        return ids

    async def _changes(self, cursor: str | None) -> ChangeBatch:
        body = await self._post_json(self._changes_url, {"cursor": cursor})
        user_ids = body.get("user_ids")
        next_cursor = body.get("cursor")
        reset = body.get("reset", False)
        if (
            not isinstance(user_ids, list)
            or any(not isinstance(u, str) for u in user_ids)
            or not isinstance(next_cursor, str)
            or not next_cursor
            or not isinstance(reset, bool)
        ):
            raise ValueError("invalid HTTP authorization changes")
        return ChangeBatch(frozenset(user_ids), next_cursor, reset=reset or cursor is None)

    async def aclose(self) -> None:
        await self._client.aclose()


def create(context: AuthzContext) -> HttpBackend:
    """`lumewarp.authz` entry point factory."""
    if context.settings is None:
        raise RuntimeError("the http authz backend needs settings")
    return HttpBackend(context.settings)
