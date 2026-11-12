"""A `lumewarp.authz` backend that reads grants from an external directory service.

The control plane asks it one question: does this user hold this relation on this org or
team? It answers direct relations only: Lumewarp applies org-to-team inheritance itself, so
an org `admin` must not be reported as a team `admin` or `member`. A team `admin` implies
`member`. A failure is `Decision.ERROR` (the admin API answers 503), never a deny.

This example calls `GET {MY_AUTHZ_DIRECTORY_URL}/users/{user_id}/grants`, which answers
`[{"relation": "admin", "type": "team", "id": "<uuid>"}, ...]`, and 404 for a user it does not
know. Replace `_grants` with whatever your system offers (LDAP groups, an IAM API, ...).
"""

import os
import uuid
from typing import Literal

import httpx
from lumewarp_control_plane.authz import Decision, Relation, Resource

Grant = tuple[str, str, str]  # (relation, resource type, resource id)


class DirectoryBackend:
    # Memberships live in the directory: Lumewarp's member write endpoints answer
    # 409 managed_externally, and the key reconciler suspends keys of removed members.
    manages_grants = False

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _grants(self, user_id: str) -> set[Grant]:
        """Raises on any failure; the control plane turns that into `Decision.ERROR`."""
        response = await self._client.get(f"/users/{user_id}/grants")
        if response.status_code == 404:
            return set()
        response.raise_for_status()
        return {(g["relation"], g["type"], str(uuid.UUID(g["id"]))) for g in response.json()}

    @staticmethod
    def _holds(grants: set[Grant], relation: Relation, type_: str, id_: str) -> bool:
        if (relation.value, type_, id_) in grants:
            return True
        # A team admin is also a member of that team. Nothing else is implied.
        return relation is Relation.MEMBER and type_ == "team" and ("admin", "team", id_) in grants

    async def check(self, user_id: str, relation: Relation, resource: Resource) -> Decision:
        try:
            grants = await self._grants(user_id)
        except Exception:
            return Decision.ERROR
        allowed = self._holds(grants, relation, resource.type, str(uuid.UUID(resource.id)))
        return Decision.ALLOW if allowed else Decision.DENY

    async def list_objects(
        self, user_id: str, relation: Relation, resource_type: Literal["org", "team"]
    ) -> set[str]:
        """Optional: one directory call instead of a `check` per candidate. Raising is an
        error, never an empty list."""
        grants = await self._grants(user_id)
        return {
            id_ for _, type_, id_ in grants if self._holds(grants, relation, resource_type, id_)
        }


def create() -> DirectoryBackend:
    """Entry point factory, called once at startup. A missing setting aborts startup."""
    base_url = os.environ["MY_AUTHZ_DIRECTORY_URL"]
    return DirectoryBackend(httpx.AsyncClient(base_url=base_url, timeout=5.0))
