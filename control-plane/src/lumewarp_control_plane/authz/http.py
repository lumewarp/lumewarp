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

Lumewarp never writes to the service (`manages_grants = False`), so the member endpoints of
the admin API are read-only under this backend.
"""

import httpx

from lumewarp_control_plane.authz import AuthzContext, Decision, Relation, Resource
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

    async def aclose(self) -> None:
        await self._client.aclose()


def create(context: AuthzContext) -> HttpBackend:
    """`lumewarp.authz` entry point factory."""
    if context.settings is None:
        raise RuntimeError("the http authz backend needs settings")
    return HttpBackend(context.settings)
