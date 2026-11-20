"""Session-only Playground proxy; raw API key secrets never enter the browser."""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response, StreamingResponse

from lumewarp_control_plane.api.common import SessionDep, SettingsDep
from lumewarp_control_plane.api.keys import _live_team_or_404, _owner_effective
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.validate import accepted_reasoning_levels
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import (
    Alias,
    ApiKey,
    CatalogName,
    ConfigVersion,
    KeyLifecycle,
    KeyOwnerKind,
    Model,
    Org,
    PlaygroundRequest,
    Router,
    RouterFallback,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
    ServiceAccount,
    Team,
    Zone,
)
from lumewarp_control_plane.quota import key_models
from lumewarp_control_plane.security import PrincipalKind

log = logging.getLogger(__name__)
router = APIRouter(tags=["playground"])


def mint_token(encryption_key: str, key_id: uuid.UUID, exp_ms: int) -> str:
    """Mint the domain-separated, short-lived gateway delegation token."""
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"lumewarp-playground-v1"
    ).derive(encryption_key.encode())
    message = f"lwpg.{key_id}.{exp_ms}"
    mac = base64.urlsafe_b64encode(hmac.digest(key, message.encode(), hashlib.sha256))
    return f"{message}.{mac.decode().rstrip('=')}"


class PlaygroundKey(BaseModel):
    id: uuid.UUID
    zone: str
    name: str
    display_prefix: str
    team_id: uuid.UUID
    team_name: str
    org_id: uuid.UUID = Field(description="The id of the team's org.")
    org_name: str = Field(description="The name of the team's org.")
    models: list[str]
    owner_kind: KeyOwnerKind
    service_account_name: str | None


class PlaygroundReasoning(BaseModel):
    """The reasoning_effort levels a name maps, and the one used when a request names none."""

    levels: list[str]
    default_level: str | None


class PlaygroundOut(BaseModel):
    enabled: bool
    keys: list[PlaygroundKey]
    # By callable name: mapped models, routers (their members' intersection) and aliases of
    # either. A name that accepts any level is offered the standard OpenAI levels.
    reasoning: dict[str, PlaygroundReasoning] = {}


# Offered for a name whose accepted set is "any level".
STANDARD_REASONING_LEVELS = ("minimal", "low", "medium", "high")


def _levels(accepted: list[str] | None, default: str | None) -> list[str]:
    """The levels the picker offers: the intersection, or the standard ones when it is any."""
    if accepted is not None:
        return accepted
    levels = list(STANDARD_REASONING_LEVELS)
    if default is not None and default not in levels:
        levels.append(default)
    return levels


async def _reasoning(session) -> dict[str, PlaygroundReasoning]:
    models = (
        await session.execute(
            select(
                Model.name_id,
                Model.reasoning_mode,
                Model.reasoning_fragments,
                Model.reasoning_default_level,
            )
        )
    ).all()
    reasoning_by_id: dict[uuid.UUID, PlaygroundReasoning] = {
        row.name_id: PlaygroundReasoning(
            levels=sorted(row.reasoning_fragments or {}),
            default_level=row.reasoning_default_level,
        )
        for row in models
        if row.reasoning_mode == "mapped"
    }
    reasoning_by_model = {
        row.name_id: (row.reasoning_mode, row.reasoning_fragments) for row in models
    }
    # A router can call its splits' members (its own member list is ignored then) and its
    # fallbacks; every one of them must accept the level for the router to offer it.
    pool: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    split_routers = set(await session.scalars(select(RouterSplit.router_id)))
    for router_id, model_id in await session.execute(
        select(RouterMember.router_id, RouterMember.model_id)
    ):
        if router_id not in split_routers:
            pool[router_id].append(model_id)
    for router_id, model_id in await session.execute(
        select(RouterSplit.router_id, RouterSplitMember.model_id).join(
            RouterSplitMember, RouterSplit.id == RouterSplitMember.split_id
        )
    ):
        pool[router_id].append(model_id)
    for router_id, model_id in await session.execute(
        select(RouterFallback.router_id, RouterFallback.model_id)
    ):
        pool[router_id].append(model_id)
    for row in await session.execute(select(Router.name_id, Router.reasoning_default_level)):
        accepted = accepted_reasoning_levels(
            reasoning_by_model[model_id]
            for model_id in pool[row.name_id]
            if model_id in reasoning_by_model
        )
        reasoning_by_id[row.name_id] = PlaygroundReasoning(
            levels=_levels(accepted, row.reasoning_default_level),
            default_level=row.reasoning_default_level,
        )
    names = {
        id_: name for id_, name in await session.execute(select(CatalogName.id, CatalogName.name))
    }
    out = {names[id_]: item for id_, item in reasoning_by_id.items() if id_ in names}
    aliases = await session.execute(
        select(CatalogName.name, Alias.target_name_id)
        .join(CatalogName, CatalogName.id == Alias.name_id)
        .where(Alias.target_name_id.in_(reasoning_by_id))
    )
    for name, target in aliases.all():
        out[name] = reasoning_by_id[target]
    return out


async def _llm_names(session: SessionDep) -> set[str]:
    from lumewarp_control_plane.model_types import catalog_types

    types = await catalog_types(session)
    return {
        name
        for id_, name in (await session.execute(select(CatalogName.id, CatalogName.name))).all()
        if types.get(id_) == "llm"
    }


async def _zone_names(session: AsyncSession, zone: str) -> set[str]:
    """Names the zone's gateways serve: models, routers and aliases of its latest snapshot.

    The compiler emits a model only where it has a deployment (and a router or alias only
    when what it calls is there), so this is what a key of the zone can actually call.
    """
    from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2

    payload = await session.scalar(
        select(ConfigVersion.payload)
        .where(ConfigVersion.zone == zone, ConfigVersion.payload.is_not(None))
        .order_by(ConfigVersion.version.desc())
        .limit(1)
    )
    if payload is None:
        return set()
    snapshot = snapshot_pb2.Snapshot.FromString(payload)
    return (
        {model.name for model in snapshot.models}
        | {router.name for router in snapshot.routers}
        | {alias.name for alias in snapshot.aliases}
    )


def _session_only(authz: Authz) -> None:
    if authz.principal.kind is not PrincipalKind.SESSION:
        raise HTTPException(401, "a user session is required")


def _usable(key: ApiKey) -> bool:
    return key.lifecycle is KeyLifecycle.ACTIVE and (
        key.expires_at is None or key.expires_at > datetime.now(UTC)
    )


async def _record_request(
    session: AsyncSession,
    raw_request_id: str | None,
    user_id: uuid.UUID | None,
    key_id: uuid.UUID,
) -> None:
    """Attribute a Playground call to its user once the gateway's headers have arrived.

    The gateway names the request; a missing or malformed id is skipped. Writing this side
    table never fails a call that already reached the gateway.
    """
    if raw_request_id is None:
        log.debug("gateway response has no x-lumewarp-request-id; attribution skipped")
        return
    try:
        request_id = uuid.UUID(raw_request_id)
    except ValueError:
        log.debug("gateway response has an invalid x-lumewarp-request-id; attribution skipped")
        return
    if user_id is None:
        log.debug("Playground session has no user; attribution skipped")
        return
    try:
        await session.execute(
            pg_insert(PlaygroundRequest)
            .values(request_id=request_id, user_id=user_id, key_id=key_id)
            .on_conflict_do_nothing(index_elements=["request_id"])
        )
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        log.warning("failed to record the Playground request", exc_info=True)


@router.get("/playground", operation_id="getPlayground")
async def get_playground(authz: Authz, session: SessionDep, settings: SettingsDep) -> PlaygroundOut:
    """The caller's usable keys, their own plus service-account keys of teams they administer,
    and the `llm` names each may call, for the Admin UI Playground. Requires a browser session;
    a personal access token answers 401.
    """
    _session_only(authz)
    live_team_ids = list(await session.scalars(select(Team.id).where(Team.deleted_at.is_(None))))
    # Direct team admins plus org and installation inheritance, in one batched check.
    administered = await authz.admin_teams(live_team_ids)
    administered_team_ids = live_team_ids if administered is None else list(administered)
    rows = (
        await session.execute(
            select(ApiKey, ServiceAccount.name, Team, Org.name)
            .join(Team, Team.id == ApiKey.team_id)
            .join(Org, Org.id == Team.org_id)
            .outerjoin(ServiceAccount, ServiceAccount.id == ApiKey.service_account_id)
            .where(
                Team.deleted_at.is_(None),
                or_(
                    and_(
                        ApiKey.owner_kind == KeyOwnerKind.USER,
                        ApiKey.user_id == authz.user_id,
                    ),
                    and_(
                        ApiKey.owner_kind == KeyOwnerKind.SERVICE_ACCOUNT,
                        ApiKey.team_id.in_(administered_team_ids),
                        ServiceAccount.deactivated_at.is_(None),
                    ),
                ),
            )
            .order_by(ApiKey.id)
        )
    ).all()
    zones = {
        row.id: row for row in await session.scalars(select(Zone).where(Zone.archived_at.is_(None)))
    }
    # Only keys in a zone that allows the Playground and has a gateway URL are offered.
    usable_zones = {
        name: row
        for name, row in zones.items()
        if row.playground_allowed and row.playground_gateway_url
    }
    keys = []
    llm_names = await _llm_names(session)
    served: dict[str, set[str]] = {}
    for key, account_name, team, org_name in rows:
        if not _usable(key):
            continue
        zone_row = usable_zones.get(key.zone)
        if zone_row is None:
            continue
        if key.owner_kind is KeyOwnerKind.USER and not await authz.allowed(
            Action.KEY_MANAGE_OWN, team.id
        ):
            continue
        # What the key itself may call: the owner's set, narrowed by a key `list`.
        key_set = key_models((await _owner_effective(session, team, key)).models, key)
        models = sorted(llm_names) if key_set is None else sorted(key_set)
        if key.zone not in served:
            served[key.zone] = await _zone_names(session, key.zone)
        keys.append(
            PlaygroundKey(
                id=key.id,
                zone=key.zone,
                name=key.name,
                display_prefix=key.display_prefix,
                team_id=team.id,
                team_name=team.name,
                org_id=team.org_id,
                org_name=org_name,
                models=sorted(set(models) & llm_names & served[key.zone]),
                owner_kind=key.owner_kind,
                service_account_name=account_name,
            )
        )
    callable_names = {name for key in keys for name in key.models}
    reasoning = {
        name: levels
        for name, levels in (await _reasoning(session)).items()
        if name in callable_names
    }
    return PlaygroundOut(
        enabled=bool(usable_zones and settings.encryption_key),
        keys=keys,
        reasoning=reasoning,
    )


@router.post(
    "/playground/v1/chat/completions",
    operation_id="playgroundChatCompletions",
    response_class=Response,
    responses={
        200: {
            "description": "The gateway's response, passed through",
            "content": {
                "application/json": {"schema": {"type": "object"}},
                "text/event-stream": {"schema": {"type": "string"}},
            },
        }
    },
)
async def chat_completions(
    request: Request, authz: Authz, session: SessionDep, settings: SettingsDep
) -> Response:
    """Forward a chat completion to the gateway as one of the caller's own keys, or as a
    service-account key of a team they administer, named by its id in the `X-Lumewarp-Act-As`
    header. The body and the response, streamed or not, are the gateway's
    `/v1/chat/completions`. Requires a browser session, accepts `llm` names only and at most
    1 MiB, and answers 503 `playground_disabled` when no gateway URL is configured.
    """
    _session_only(authz)
    try:
        key_id = uuid.UUID(request.headers.get("x-lumewarp-act-as", ""))
    except ValueError:
        raise HTTPException(404, "key not found") from None
    key = await session.get(ApiKey, key_id)
    if key is None or not _usable(key):
        raise HTTPException(404, "key not found")
    if key.owner_kind is KeyOwnerKind.USER:
        if key.user_id != authz.user_id:
            raise HTTPException(404, "key not found")
        team = await _live_team_or_404(session, key.team_id)
        if not await authz.allowed(Action.KEY_MANAGE_OWN, key.team_id):
            raise HTTPException(404, "key not found")
    elif key.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT:
        team = await _live_team_or_404(session, key.team_id)
        account = (
            await session.get(ServiceAccount, key.service_account_id)
            if key.service_account_id is not None
            else None
        )
        if account is None or not account.is_active:
            raise HTTPException(404, "key not found")
        if not await authz.allowed(Action.KEY_MANAGE_ANY, key.team_id):
            raise HTTPException(404, "key not found")
    else:
        raise HTTPException(404, "key not found")
    zone = await session.get(Zone, key.zone)
    if zone is None or zone.archived_at is not None:
        raise ApiError(403, "playground_not_allowed", "the key's zone is not available")
    if not zone.playground_allowed:
        raise ApiError(
            403, "playground_not_allowed", f"zone {zone.id!r} does not allow the Playground"
        )
    playground_gateway_url = zone.playground_gateway_url
    if not playground_gateway_url or not settings.encryption_key:
        raise ApiError(
            503, "playground_disabled", f"Playground is not configured in zone {zone.id!r}"
        )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "request body exceeds 1 MiB")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "invalid JSON body") from None
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str):
        key_set = key_models((await _owner_effective(session, team, key)).models, key)
        allowed = (
            await session.scalar(select(CatalogName.name).where(CatalogName.name == model))
            is not None
            if key_set is None
            else model in key_set
        )
        if allowed:
            from lumewarp_control_plane.model_types import called_name_type

            if await called_name_type(session, model) != "llm":
                raise ApiError(400, "model_type_mismatch", "Playground supports only llm models")
    # Close the read transaction before the gateway call: the response body is streamed and
    # must not hold a database connection.
    await session.commit()
    token = mint_token(
        settings.encryption_key.get_secret_value(), key.id, time.time_ns() // 1_000_000 + 60_000
    )
    client: httpx.AsyncClient = request.app.state.playground_client
    try:
        upstream = await client.send(
            client.build_request(
                "POST",
                playground_gateway_url.rstrip("/") + "/v1/chat/completions",
                content=bytes(body),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            ),
            stream=True,
        )
    except httpx.RequestError:
        raise ApiError(502, "playground_gateway_unreachable", "gateway unreachable") from None

    await _record_request(
        session, upstream.headers.get("x-lumewarp-request-id"), authz.user_id, key.id
    )

    chunks = upstream.aiter_raw()
    prefix: list[bytes] = []
    if upstream.status_code == 401:
        # Only inspect small authentication errors; never buffer an arbitrary response.
        size = 0
        try:
            async for chunk in chunks:
                prefix.append(chunk)
                size += len(chunk)
                if size > 64 * 1024:
                    break
            else:
                try:
                    error = json.loads(b"".join(prefix))
                except (ValueError, UnicodeDecodeError):
                    error = None
                if (
                    isinstance(error, dict)
                    and isinstance(error.get("error"), dict)
                    and error["error"].get("code") == "invalid_api_key"
                ):
                    await upstream.aclose()
                    if not request.app.state.playground_warned_misconfigured:
                        request.app.state.playground_warned_misconfigured = True
                        log.warning(
                            "Playground gateway rejected delegation; check shared configuration"
                        )
                    raise ApiError(
                        502, "playground_misconfigured", "Playground gateway configuration mismatch"
                    )
        except httpx.RequestError:
            await upstream.aclose()
            raise ApiError(502, "playground_gateway_unreachable", "gateway unreachable") from None
        except BaseException:
            await upstream.aclose()
            raise

    async def stream():
        try:
            for chunk in prefix:
                yield chunk
            async for chunk in chunks:
                yield chunk
        finally:
            await upstream.aclose()

    headers = {
        name: upstream.headers[name]
        for name in ("content-type", "content-encoding", "x-lumewarp-request-id")
        if name in upstream.headers
    }
    return StreamingResponse(stream(), status_code=upstream.status_code, headers=headers)
