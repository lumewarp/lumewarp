"""Authorization delegated to an HTTP service."""

import httpx

from lumewarp_control_plane.authz import Decision, Relation, Resource
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
