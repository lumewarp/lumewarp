"""Audit log writer. Never records secrets, Authorization headers or prompts.

Whether a detail field may be stored is decided per action, by the allowlist below: a
field that is not listed is dropped, not redacted. Adding a detail therefore requires
classifying it here first, and a test scans the source so a new action cannot bypass the
table. `redact` stays as a second line of defence for sensitive-looking key names.

Free-text error columns elsewhere (`config_outbox.last_error`) store only the exception
class name.
"""

import enum
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import ActorType, AuditLog, Team
from lumewarp_control_plane.security import Principal

log = logging.getLogger(__name__)
REDACTED = "[redacted]"
_SENSITIVE = ("secret", "token", "password", "authorization", "api_key", "key_hash", "prompt")

# A detail schema: None records a scalar (or a list of scalars) as is, a mapping lists the
# only keys that may be stored, and a one-element list applies its schema to list items.
type DetailSpec = dict[str, DetailSpec] | list[DetailSpec] | None

_POLICY: dict[str, DetailSpec] = {
    "model_access": None,
    "allowed_models": None,
    "rpm_limit": None,
    "tpm_limit": None,
    "max_parallel": None,
    "budget_nano_usd": None,
}
_POLICY_CHANGES: dict[str, DetailSpec] = {
    **_POLICY,
    "name": None,
    "description": None,
    "role": None,
    "is_active": None,
}
_HEALTH_CHECK: dict[str, DetailSpec] = {
    "interval_ms": None,
    "timeout_ms": None,
    "unhealthy_threshold": None,
    "healthy_threshold": None,
}
_CIRCUIT_BREAKER: dict[str, DetailSpec] = {"failure_threshold": None, "open_ms": None}
_RETRY: dict[str, DetailSpec] = {
    "max_attempts": None,
    "retry_on_status": None,
    "total_timeout_ms": None,
}
_ROUTER_MEMBER: dict[str, DetailSpec] = {"model_id": None, "weight": None}
_TRAFFIC_SPLIT: dict[str, DetailSpec] = {
    "name": None,
    "weight": None,
    "members": [_ROUTER_MEMBER],
}
_ROUTER_SETTINGS: dict[str, DetailSpec] = {
    "strategy": None,
    "retry": _RETRY,
    "fallback_model_ids": None,
    "traffic_split": [_TRAFFIC_SPLIT],
    "sticky": None,
    "sticky_header": None,
}

# Every `audit.record(action=...)` call site must have an entry here; an unknown action
# records no details at all. Keep this table in sync with the source (tests enforce it).
DETAIL_ALLOWLIST: dict[str, dict[str, DetailSpec]] = {
    "alias.create": {"name": None, "target": None, "kept_from_rename": None, "grants_copied": None},
    "alias.delete": {"name": None},
    "alias.rename": {"old_name": None, "new_name": None},
    "alias.update": {"name": None, "changes": {"name": None, "target": None}},
    "budget.hard_exceeded": {
        "window_start": None,
        "spent_nano_usd": None,
        "budget_nano_usd": None,
    },
    "budget.monthly_rollover": {"window_start": None},
    "config.rollback": {"to_version": None, "force": None},
    "gateway_token.create": {"gateway_id": None, "name": None},
    "gateway_token.revoke": {"gateway_id": None, "name": None},
    "installation_admin.assign": {"user_id": None, "role": None},
    "installation_admin.bootstrap": {"user_id": None, "role": None},
    "installation_admin.remove": {"user_id": None, "role": None},
    "installation_admin.revoke_tokens": {"user_id": None, "revoked": None},
    "installation_admin.transfer": {
        "previous_user_id": None,
        "new_user_id": None,
        "revoked": None,
    },
    "grant.delete": {"name": None, "name_id": None, "user_id": None, "service_account_id": None},
    "key.create": {"name": None, "owner_kind": None, "owner_id": None, "expires_at": None},
    "key.revoke": {"name": None},
    "key.rotate": {
        "name": None,
        "grace_seconds": None,
        "expires_at": None,
        "previous_key_expires_at": None,
    },
    "key.suspend": {"name": None, "user_id": None, "reason": None},
    "key.update": {"name": None, "changes": {"name": None, "expires_at": None, "lifecycle": None}},
    "member.add": {"user_id": None, "role": None},
    "member.remove": {"user_id": None, "suspended_keys": None},
    "member.update": {
        "user_id": None,
        "role": None,
        "policy_changed": None,
        "changes": _POLICY_CHANGES,
    },
    "model.create": {"name": None, "upstream": None},
    "model.delete": {"name": None},
    "model.rename": {"old_name": None, "new_name": None},
    "model.update": {
        "name": None,
        "changes": {
            "name": None,
            "upstream_id": None,
            "upstream_model": None,
            "price_input_nano_usd_per_mtok": None,
            "price_output_nano_usd_per_mtok": None,
            "keep_old_name_as_alias": None,
            "max_input_tokens": None,
            "max_output_tokens": None,
            "default_body_changed": None,
            "reasoning_changed": None,
            "chat_check": {
                "interval_ms": None,
                "timeout_ms": None,
                "unhealthy_threshold": None,
                "healthy_threshold": None,
            },
        },
    },
    "org.admin.assign": {"org_id": None, "user_id": None},
    "org.admin.remove": {"org_id": None, "user_id": None},
    "org.archive": {"name": None},
    "org.create": {"name": None},
    "org.update": {"name": None, "policy_changed": None, "changes": _POLICY_CHANGES},
    "pat.create": {"name": None, "user_id": None},
    "pat.revoke": {"name": None},
    "playground.request": {"key_id": None, "model": None},
    "router.create": {"name": None, "members": None, "settings": _ROUTER_SETTINGS},
    "router.delete": {"name": None},
    "router.force_save": {"name": None, "strategy": None, "missing_gateways": None},
    "router.rename": {"old_name": None, "new_name": None},
    "router.update": {
        "name": None,
        "changes": {**_ROUTER_SETTINGS, "name": None, "members": [_ROUTER_MEMBER]},
    },
    "service_account.create": {"name": None},
    "service_account.delete": {"name": None},
    "service_account.update": {
        "name": None,
        "policy_changed": None,
        "changes": _POLICY_CHANGES,
    },
    "session.break_glass.login": {"result": None, "window_seconds": None},
    "session.break_glass.request": {"method": None, "path": None, "status": None},
    "team.create": {"name": None},
    "team.archive": {
        "name": None,
        "revoked_keys": None,
        "deactivated_service_accounts": None,
    },
    "team.update": {
        "name": None,
        "policy_changed": None,
        "changes": {
            **_POLICY_CHANGES,
            "max_keys_per_member": None,
            "max_member_key_lifetime_seconds": None,
        },
    },
    "upstream.create": {"name": None, "base_url": None, "credential_stored": None},
    "upstream.delete": {"name": None},
    "upstream.update": {
        "name": None,
        "credential_changed": None,
        "changes": {
            "name": None,
            "kind": None,
            "base_url": None,
            "api_key_ref": None,
            "timeout_ms": None,
            "stream_usage_mode": None,
            "health_url": None,
            "health_check": _HEALTH_CHECK,
            "circuit_breaker": _CIRCUIT_BREAKER,
            "metrics_url": None,
        },
    },
    "user.create": {"email": None},
    "user.delete": {"email": None},
    "user.update": {
        "email": None,
        "suspended_keys": None,
        "changes": {"email": None, "display_name": None, "is_active": None},
    },
}

# Names that match `_SENSITIVE` but hold no secret: an env var name and token counts.
_NOT_SENSITIVE = frozenset({"api_key_ref", "max_input_tokens", "max_output_tokens"})

_DROP = object()


def redact(value: Any) -> Any:
    """Recursively replace values under sensitive-looking keys, except the known
    non-secrets in `_NOT_SENSITIVE`.
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            lowered = key.lower()
            if lowered not in _NOT_SENSITIVE and any(s in lowered for s in _SENSITIVE):
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


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return str(value)
    # Anything else is not a classified scalar: storing it could leak a repr.
    return REDACTED


def _filter(value: Any, spec: DetailSpec) -> Any:
    if spec is None:
        if isinstance(value, Mapping):
            return _DROP
        if isinstance(value, list | tuple):
            items = [_filter(item, None) for item in value]
            return _DROP if any(item is _DROP for item in items) else items
        return _scalar(value)
    if isinstance(spec, dict):
        if value is None:
            return None
        if not isinstance(value, Mapping):
            return _DROP
        kept: dict[str, Any] = {}
        for key, item_spec in spec.items():
            if key not in value:
                continue
            filtered = _filter(value[key], item_spec)
            if filtered is not _DROP:
                kept[key] = filtered
        return kept
    if value is None:  # an explicit null removes a nested setting
        return None
    if not isinstance(value, list | tuple):
        return _DROP
    kept_items = []
    for item in value:
        filtered = _filter(item, spec[0])
        if filtered is not _DROP:
            kept_items.append(filtered)
    return kept_items


def details_of(action: str, details: Mapping[str, Any] | None) -> dict[str, Any]:
    """Apply the action's allowlist, then the sensitive-name redaction."""
    if not details:
        return {}
    spec = DETAIL_ALLOWLIST.get(action)
    if spec is None:
        log.warning("audit action %r has no detail allowlist; details were dropped", action)
        return {}
    kept = _filter(details, spec)
    return redact(kept) if isinstance(kept, dict) else {}


def actor_of(principal: Principal) -> tuple[ActorType, str | None]:
    if principal.user_id is None:
        return ActorType.BREAK_GLASS, None
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
    team_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    details: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Add an audit row to the caller's transaction (commit together with the change).

    Pass `principal` for API callers, or `actor_type`/`actor_id` for gateways and system
    jobs (e.g. ActorType.SYSTEM, "budget"). A team-scoped row gets the team's current org,
    read in the same INSERT so no call site can forget it; a caller that deletes the team
    in the same transaction passes `org_id` explicitly.
    """
    if principal is not None:
        actor_type, actor_id = actor_of(principal)
    if actor_type is None:
        raise ValueError("audit.record needs a principal or an actor_type")
    if team_id is not None and org_id is None:
        org_id = select(Team.org_id).where(Team.id == team_id).scalar_subquery()
    row = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        team_id=team_id,
        org_id=org_id,
        details=details_of(action, details),
    )
    session.add(row)
    return row
