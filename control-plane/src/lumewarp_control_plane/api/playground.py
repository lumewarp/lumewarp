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
from sqlalchemy import select
from starlette.responses import Response, StreamingResponse

from lumewarp_control_plane import audit
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


def _session_only(authz: Authz) -> None:
    if authz.principal.kind is not PrincipalKind.SESSION:
        raise HTTPException(401, "a user session is required")


def _usable(key: ApiKey) -> bool:
    return key.lifecycle is KeyLifecycle.ACTIVE and (
        key.expires_at is None or key.expires_at > datetime.now(UTC)
    )


@router.get("/playground", operation_id="getPlayground")
async def get_playground(authz: Authz, session: SessionDep, settings: SettingsDep) -> PlaygroundOut:
    _session_only(authz)
    keys = []
    rows = await session.scalars(
        select(ApiKey).where(
            ApiKey.user_id == authz.user_id, ApiKey.owner_kind == KeyOwnerKind.USER
        )
    )
    for key in rows:
        if not _usable(key):
            continue
        # Archived teams retain their keys for historical usage.
        team = await session.get(Team, key.team_id)
        if team is None or team.deleted_at is not None:
            continue
        if not await authz.allowed(Action.KEY_MANAGE_OWN, team.id):
            continue
        effective = await _owner_effective(session, team, key)
        models = (
            sorted((await session.scalars(select(CatalogName.name))).all())
            if effective.models is None
            else sorted(effective.models)
        )
        keys.append(
            PlaygroundKey(
                id=key.id,
                name=key.name,
                display_prefix=key.display_prefix,
                team_id=team.id,
                team_name=team.name,
                models=models,
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


@router.post("/playground/v1/chat/completions", operation_id="playgroundChatCompletions")
async def chat_completions(
    request: Request, authz: Authz, session: SessionDep, settings: SettingsDep
) -> Response:
    _session_only(authz)
    try:
        key_id = uuid.UUID(request.headers.get("x-lumewarp-act-as", ""))
    except ValueError:
        raise HTTPException(404, "key not found") from None
    key = await session.get(ApiKey, key_id)
    if (
        key is None
        or key.owner_kind is not KeyOwnerKind.USER
        or key.user_id != authz.user_id
        or not _usable(key)
    ):
        raise HTTPException(404, "key not found")
    team = await _live_team_or_404(session, key.team_id)
    if not await authz.allowed(Action.KEY_MANAGE_OWN, key.team_id):
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
    audited_model = None
    if isinstance(model, str) and len(model) <= 256:
        effective = await _owner_effective(session, team, key)
        allowed = (
            await session.scalar(select(CatalogName.name).where(CatalogName.name == model))
            is not None
            if effective.models is None
            else model in effective.models
        )
        if allowed:
            audited_model = model
    audit.record(
        session,
        action="playground.request",
        resource_type="key",
        resource_id=key.id,
        principal=authz.principal,
        team_id=key.team_id,
        details={"key_id": key.id, "model": audited_model},
    )
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
