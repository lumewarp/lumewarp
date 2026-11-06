"""OpenFGA authorization and tuple changes over its HTTP API.

An existing store can be attached without renaming anything, but a store that is already
deployed often names its objects differently. `LUMEWARP_OPENFGA_OBJECT_TYPES` and
`LUMEWARP_OPENFGA_RELATIONS` map Lumewarp's `(resource type, relation)` to the store's
`(object type, relation)`; every request path uses the same `ObjectMapping`:

    Lumewarp                      store (example)
    org      --(admin)------>     llm_org  --(admin)
    team     --(admin,member)->   llm_team --(owner, member)

`deploy/openfga/model.fga` is the standalone model with the default names, and
`model-renamed.fga` is the same model under `llm_org` / `llm_team` for this mapping.

The backend answers direct relations only and never writes tuples (`manages_grants =
False`). `changes()` maps a change back to the Lumewarp users whose key ownership may have
changed; a change that cannot be mapped to users (a userset or wildcard subject) asks the
reconciler for a full rescan instead of being dropped.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx

from lumewarp_control_plane.authz import (
    AuthzContext,
    ChangeBatch,
    CheckItem,
    Decision,
    Relation,
    Resource,
)
from lumewarp_control_plane.settings import Settings

CHANGES_MAX_PAGES = 20
CHANGES_MARGIN = timedelta(seconds=5)
# A store object type / relation is an OpenFGA identifier.
FGA_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
LUMEWARP_TYPES = ("org", "team")
log = logging.getLogger(__name__)


class ObjectMapping:
    """Lumewarp `(resource type, relation)` <-> store `(object type, relation)`.

    Defaults are identity, so an existing store with today's names needs no configuration.
    Mapping both Lumewarp types to one store type is rejected: a change could no longer be
    attributed to one of them.
    """

    def __init__(
        self,
        object_types: Mapping[str, str] | None = None,
        relations: Mapping[str, str] | None = None,
    ) -> None:
        self._object_types: dict[str, str] = {}
        for resource_type, object_type in (object_types or {}).items():
            if resource_type not in LUMEWARP_TYPES:
                raise ValueError(f"unknown Lumewarp resource type {resource_type!r}")
            if not FGA_NAME.fullmatch(object_type):
                raise ValueError(f"invalid OpenFGA object type {object_type!r}")
            self._object_types[resource_type] = object_type
        if len(set(self._object_types.values())) != len(self._object_types):
            raise ValueError("two Lumewarp resource types cannot map to one OpenFGA type")
        self._relations: dict[tuple[str, Relation], str] = {}
        for key, replacement in (relations or {}).items():
            resource_type, _, relation = key.partition(".")
            try:
                known_relation = Relation(relation)
            except ValueError:
                raise ValueError(f"invalid relation in mapping key {key!r}") from None
            if resource_type not in LUMEWARP_TYPES:
                raise ValueError(f"unknown Lumewarp resource type in mapping key {key!r}")
            if not FGA_NAME.fullmatch(replacement):
                raise ValueError(f"invalid OpenFGA relation {replacement!r}")
            self._relations[(resource_type, known_relation)] = replacement
        # A store relation can only be attributed back to one Lumewarp relation.
        for (resource_type, relation), store_relation in self._relations.items():
            twins = [
                other
                for (other_type, other_relation), other in self._relations.items()
                if other_type == resource_type
                and other == store_relation
                and other_relation is not relation
            ]
            if twins:
                raise ValueError(
                    f"OpenFGA relation {store_relation!r} maps to two Lumewarp relations of "
                    f"{resource_type!r}"
                )

    def object_type(self, resource_type: str) -> str:
        return self._object_types.get(resource_type, resource_type)

    def relation(self, resource_type: str, relation: Relation) -> str:
        return self._relations.get((resource_type, relation), relation)

    def lumewarp_object(self, store_object: str) -> tuple[str, str] | None:
        """`(Lumewarp resource type, id)` for a store object, or None if it is not ours."""
        store_type, _, object_id = store_object.partition(":")
        if not object_id:
            return None
        for resource_type in LUMEWARP_TYPES:
            if self.object_type(resource_type) == store_type:
                return resource_type, object_id
        return None

    def lumewarp_relation(self, resource_type: str, store_relation: str) -> Relation | None:
        """The Lumewarp relation a store relation stands for, or None if Lumewarp never
        asks about it.
        """
        for relation in Relation:
            if self.relation(resource_type, relation) == store_relation:
                return relation
        return None


def _user_subject(subject: object) -> str | None:
    """The Lumewarp user id of a plain `user:<uuid>` subject, else None."""
    if not isinstance(subject, str) or not subject.startswith("user:"):
        return None
    try:
        return str(uuid.UUID(subject.removeprefix("user:")))
    except ValueError:
        return None


class OpenFGABackend:
    manages_grants = False

    def __init__(self, settings: Settings) -> None:
        if not all((settings.openfga_url, settings.openfga_store_id, settings.openfga_model_id)):
            raise ValueError("openfga authorization requires URL, store id and model id")
        self._url = f"{settings.openfga_url.rstrip('/')}/stores/{settings.openfga_store_id}"
        self._model_id = settings.openfga_model_id
        self._timeout = settings.authz_timeout_seconds
        self._mapping = ObjectMapping(settings.openfga_object_types, settings.openfga_relations)
        headers = {}
        if settings.openfga_api_token is not None:
            headers["Authorization"] = f"Bearer {settings.openfga_api_token.get_secret_value()}"
        self._client = httpx.AsyncClient(headers=headers, timeout=settings.authz_timeout_seconds)

    def _tuple(self, user_id: str, relation: Relation, resource: Resource) -> dict[str, str]:
        return {
            "user": f"user:{user_id}",
            "relation": self._mapping.relation(resource.type, relation),
            "object": f"{self._mapping.object_type(resource.type)}:{resource.id}",
        }

    @staticmethod
    def _decision(body: object) -> Decision:
        if not isinstance(body, dict) or type(body.get("allowed")) is not bool:
            raise ValueError("invalid OpenFGA decision")
        return Decision.ALLOW if body["allowed"] else Decision.DENY

    async def _post(self, path: str, body: dict) -> dict:
        response = await self._client.post(
            f"{self._url}/{path}",
            json={"authorization_model_id": self._model_id, **body},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("invalid OpenFGA response")
        return result

    async def check(self, user_id: str, relation: Relation, resource: Resource) -> Decision:
        return self._decision(
            await self._post(
                "check",
                {
                    "tuple_key": self._tuple(user_id, relation, resource),
                    "consistency": "HIGHER_CONSISTENCY",
                },
            )
        )

    async def batch_check(self, items: Sequence[CheckItem]) -> list[Decision]:
        decisions = []
        # OpenFGA's default maximum is 50 checks per request.
        for start in range(0, len(items), 50):
            checks = [
                {"tuple_key": self._tuple(*item), "correlation_id": str(index)}
                for index, item in enumerate(items[start : start + 50])
            ]
            try:
                async with asyncio.timeout(self._timeout):
                    body = await self._post(
                        "batch-check",
                        {
                            "checks": checks,
                            "consistency": "HIGHER_CONSISTENCY",
                        },
                    )
                results = body.get("result")
                if not isinstance(results, dict):
                    raise ValueError("invalid OpenFGA batch result")
            except Exception:
                decisions.extend([Decision.ERROR] * len(checks))
                continue
            for check in checks:
                result = results.get(check["correlation_id"])
                if not isinstance(result, dict) or "error" in result:
                    decisions.append(Decision.ERROR)
                    continue
                try:
                    decisions.append(self._decision(result))
                except ValueError:
                    decisions.append(Decision.ERROR)
        return decisions

    async def list_objects(
        self, user_id: str, relation: Relation, resource_type: Literal["org", "team"]
    ) -> list[str]:
        store_type = self._mapping.object_type(resource_type)
        body = await self._post(
            "list-objects",
            {
                "user": f"user:{user_id}",
                "relation": self._mapping.relation(resource_type, relation),
                "type": store_type,
            },
        )
        objects = body.get("objects")
        prefix = f"{store_type}:"
        if not isinstance(objects, list) or any(
            not isinstance(obj, str) or not obj.startswith(prefix) for obj in objects
        ):
            raise ValueError("invalid OpenFGA objects")
        return [obj.removeprefix(prefix) for obj in objects]

    async def changes(self, cursor: str | None) -> ChangeBatch:
        reset = cursor is None
        token = cursor or ""
        users = set()
        start_time = (datetime.now(UTC) - CHANGES_MARGIN).isoformat()
        for _ in range(CHANGES_MAX_PAGES):
            params = {"continuation_token": token} if token else {"start_time": start_time}
            response = await self._client.get(f"{self._url}/changes", params=params)
            if response.status_code == 400 and token and not reset:
                # Reset near now and request a full rescan, never replay historical tuples.
                reset, token = True, ""
                users.clear()
                continue
            response.raise_for_status()
            body = response.json()
            changes = body.get("changes", [])
            next_token = body.get("continuation_token")
            if not isinstance(changes, list) or not isinstance(next_token, str):
                raise ValueError("invalid OpenFGA changes")
            for change in changes:
                key = change["tuple_key"]
                subject, relation, store_object = (
                    key["user"],
                    key["relation"],
                    key["object"],
                )
                parsed = self._mapping.lumewarp_object(store_object)
                if parsed is not None and parsed[0] != "team":
                    # A Lumewarp org relation does not decide key ownership.
                    continue
                if parsed is not None and (
                    self._mapping.lumewarp_relation(parsed[0], relation) is not None
                ):
                    # A team relation Lumewarp asks about.
                    user_id = _user_subject(subject)
                    if user_id is None:
                        # A userset or wildcard subject: the affected users are unknown, so
                        # falling back to a full rescan is the only safe answer.
                        log.debug("OpenFGA change %r affects unknown users; full rescan", subject)
                        reset = True
                        continue
                    users.add(user_id)
                    continue
                # Anything else (a non-Lumewarp object type, or a team relation Lumewarp
                # never asks about, e.g. a group whose membership grants team membership)
                # cannot be mapped to the team membership it derives. A plain user subject
                # is one cheap extra check for the reconciler; a userset or wildcard may
                # affect users we cannot name, so rescan everything.
                user_id = _user_subject(subject)
                if user_id is None:
                    log.debug("OpenFGA change %r cannot be mapped to users; full rescan", subject)
                    reset = True
                else:
                    users.add(user_id)
            if not changes or not next_token or next_token == token:
                return ChangeBatch(frozenset(users), next_token or token, reset=reset)
            token = next_token
        # Hitting the page limit is not a reset: the next poll resumes at this token.
        return ChangeBatch(frozenset(users), token, reset=reset)

    async def aclose(self) -> None:
        await self._client.aclose()


def create(context: AuthzContext) -> OpenFGABackend:
    """`lumewarp.authz` entry point factory."""
    if context.settings is None:
        raise RuntimeError("the openfga authz backend needs settings")
    return OpenFGABackend(context.settings)
