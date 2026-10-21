"""Audit log writer. Never records secrets, Authorization headers or prompts."""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import ActorType, AuditLog
from lumewarp_control_plane.security import Principal

REDACTED = "[redacted]"
_SENSITIVE = ("secret", "token", "password", "authorization", "api_key", "key_hash", "prompt")


def redact(value: Any) -> Any:
    """Recursively replace values under sensitive-looking keys (api_key_ref is kept: it is
    an env var name, not a secret).
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            lowered = key.lower()
            if lowered != "api_key_ref" and any(s in lowered for s in _SENSITIVE):
                out[key] = REDACTED
            else:
                out[key] = redact(v)
        return out
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return REDACTED
    return value


def actor_of(principal: Principal) -> tuple[ActorType, str | None]:
    if principal.user_id is None:
        return ActorType.SUPERUSER_TOKEN, None
    return ActorType.USER, str(principal.user_id)


def record(
    session: AsyncSession,
    *,
    action: str,
    resource_type: str,
    resource_id: str | uuid.UUID | None = None,
    principal: Principal | None = None,
    actor_type: ActorType | None = None,
    actor_id: str | None = None,
    org_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    details: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Add an audit row to the caller's transaction (commit together with the change).

    Pass `principal` for API callers, or `actor_type`/`actor_id` for gateways and system
    jobs (e.g. ActorType.SYSTEM, "budget").
    """
    if principal is not None:
        actor_type, actor_id = actor_of(principal)
    if actor_type is None:
        raise ValueError("audit.record needs a principal or an actor_type")
    row = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        org_id=org_id,
        team_id=team_id,
        details=redact(dict(details or {})),
    )
    session.add(row)
    return row
