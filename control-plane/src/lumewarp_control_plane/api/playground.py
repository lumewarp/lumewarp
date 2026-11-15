"""Session-only Playground proxy; raw API key secrets never enter the browser."""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import UTC, datetime

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response, StreamingResponse

from lumewarp_control_plane.api.common import SessionDep, SettingsDep
from lumewarp_control_plane.api.keys import _live_team_or_404, _owner_effective
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import (
    Alias,
    ApiKey,
    CatalogName,
    KeyLifecycle,
    KeyOwnerKind,
    Model,
    PlaygroundRequest,
    ServiceAccount,
    Team,
)
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
    name: str
    display_prefix: str
    team_id: uuid.UUID
    team_name: str
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
    # By callable name: models with mapped reasoning levels and aliases that point at one.
    # Routers are left out: their members may map different levels.
    reasoning: dict[str, PlaygroundReasoning] = {}


async def _reasoning(session) -> dict[str, PlaygroundReasoning]:
    rows = (
        await session.execute(
            select(
                Model.name_id,
                CatalogName.name,
                Model.reasoning_fragments,
                Model.reasoning_default_level,
            )
            .join(CatalogName, CatalogName.id == Model.name_id)
            .where(Model.reasoning_mode == "mapped")
        )
    ).all()
    by_id = {
        row.name_id: PlaygroundReasoning(
            levels=list(row.reasoning_fragments or {}), default_level=row.reasoning_default_level
        )
        for row in rows
    }
    out = {row.name: by_id[row.name_id] for row in rows}
    aliases = await session.execute(
        select(CatalogName.name, Alias.target_name_id)
        .join(CatalogName, CatalogName.id == Alias.name_id)
        .where(Alias.target_name_id.in_(by_id))
    )
    for name, target in aliases.all():
        out[name] = by_id[target]
    return out


async def _llm_names(session: SessionDep) -> set[str]:
    from lumewarp_control_plane.model_types import catalog_types

    types = await catalog_types(session)
    return {
        name
        for id_, name in (await session.execute(select(CatalogName.id, CatalogName.name))).all()
        if types.get(id_) == "llm"
    }


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
            select(ApiKey, ServiceAccount.name, Team)
            .join(Team, Team.id == ApiKey.team_id)
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
    keys = []
    llm_names = await _llm_names(session)
    for key, account_name, team in rows:
        if not _usable(key):
            continue
        if key.owner_kind is KeyOwnerKind.USER and not await authz.allowed(
            Action.KEY_MANAGE_OWN, team.id
        ):
            continue
        effective = await _owner_effective(session, team, key)
        models = sorted(llm_names) if effective.models is None else sorted(effective.models)
        keys.append(
            PlaygroundKey(
                id=key.id,
                name=key.name,
                display_prefix=key.display_prefix,
                team_id=team.id,
                team_name=team.name,
                models=sorted(set(models) & llm_names),
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
        enabled=bool(settings.playground_gateway_url and settings.encryption_key),
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
    if not settings.playground_gateway_url or not settings.encryption_key:
        raise ApiError(503, "playground_disabled", "Playground is not configured")
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
        effective = await _owner_effective(session, team, key)
        allowed = (
            await session.scalar(select(CatalogName.name).where(CatalogName.name == model))
            is not None
            if effective.models is None
            else model in effective.models
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
                settings.playground_gateway_url.rstrip("/") + "/v1/chat/completions",
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
