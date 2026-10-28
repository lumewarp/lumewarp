"""OpenFGA authorization and tuple changes over its HTTP API."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx

from lumewarp_control_plane.authz import ChangeBatch, CheckItem, Decision, Relation, Resource
from lumewarp_control_plane.settings import Settings

CHANGES_MAX_PAGES = 20
CHANGES_MARGIN = timedelta(seconds=5)
log = logging.getLogger(__name__)


class OpenFGABackend:
    manages_grants = False

    def __init__(self, settings: Settings) -> None:
        if not all((settings.openfga_url, settings.openfga_store_id, settings.openfga_model_id)):
            raise ValueError("openfga authorization requires URL, store id and model id")
        self._url = f"{settings.openfga_url.rstrip('/')}/stores/{settings.openfga_store_id}"
        self._model_id = settings.openfga_model_id
        self._timeout = settings.authz_timeout_seconds
        headers = {}
        if settings.openfga_api_token is not None:
            headers["Authorization"] = f"Bearer {settings.openfga_api_token.get_secret_value()}"
        self._client = httpx.AsyncClient(headers=headers, timeout=settings.authz_timeout_seconds)

    @staticmethod
    def _tuple(user_id: str, relation: Relation, resource: Resource) -> dict[str, str]:
        return {
            "user": f"user:{user_id}",
            "relation": relation,
            "object": f"{resource.type}:{resource.id}",
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

    async def list_teams(self, user_id: str, relation: Relation) -> list[str]:
        body = await self._post(
            "list-objects", {"user": f"user:{user_id}", "relation": relation, "type": "team"}
        )
        objects = body.get("objects")
        if not isinstance(objects, list) or any(
            not isinstance(obj, str) or not obj.startswith("team:") for obj in objects
        ):
            raise ValueError("invalid OpenFGA objects")
        return [obj.removeprefix("team:") for obj in objects]

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
                subject, relation, obj = key["user"], key["relation"], key["object"]
                try:
                    if not isinstance(subject, str) or not subject.startswith("user:"):
                        raise ValueError("not a user subject")
                    user_id = str(uuid.UUID(subject.removeprefix("user:")))
                except ValueError:
                    log.debug("ignoring OpenFGA change with invalid user subject %r", subject)
                    continue
                if obj.startswith("installation:") or (
                    obj.startswith("team:") and relation == "installation"
                ):
                    reset = True
                elif obj.startswith("team:") and relation in ("admin", "member"):
                    users.add(user_id)
            if not changes or not next_token or next_token == token:
                return ChangeBatch(frozenset(users), next_token or token, reset=reset)
            token = next_token
        # Hitting the page limit is not a reset: the next poll resumes at this token.
        return ChangeBatch(frozenset(users), token, reset=reset)

    async def aclose(self) -> None:
        await self._client.aclose()
