"""Routing: upstreams and the catalog (models, aliases, routers, /catalog).

Router validation mirrors the gateway (snapshot.rs::validate / validate_split and
router.rs::RoutingTable::compile) so a write the API accepts never makes the gateway NACK.
Every catalog write holds the scope lock, so the name and reference checks are exact.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import SecretStr
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit, catalog, sealing, upstream_client
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    lock_scope,
    mutation,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.policy import ensure_unique_ids
from lumewarp_control_plane.api.schemas import (
    AliasCreate,
    AliasOut,
    AliasUpdate,
    CatalogEntryOut,
    CircuitBreakerIn,
    HealthCheckIn,
    ModelCreate,
    ModelOut,
    ModelTestIn,
    ModelTestOut,
    ModelUpdate,
    Page,
    ProbeOut,
    RetryPolicyIn,
    RetryPolicyOut,
    RouterCreate,
    RouterMemberIn,
    RouterMemberOut,
    RouterOut,
    RouterUpdate,
    TrafficSplitIn,
    TrafficSplitOut,
    UpstreamCreate,
    UpstreamModelInfoOut,
    UpstreamModelsOut,
    UpstreamOut,
    UpstreamTestIn,
    UpstreamTestOut,
    UpstreamUpdate,
    validate_model_settings,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.plugins import (
    PluginRegistry,
    PluginRejected,
    audit_forced,
    load_plugins,
    member_settings,
    params_problem,
    router_problem,
    validate_plugin_params,
)
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.model_types import catalog_types
from lumewarp_control_plane.models import (
    Alias,
    CatalogKind,
    CatalogName,
    GatewaySession,
    Model,
    PluginKind,
    Router,
    RouterFallback,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
    StickyBy,
    Upstream,
)
from lumewarp_control_plane.settings import Settings

router = APIRouter(tags=["routing"])


def _non_null(body: object, *fields: str) -> None:
    supplied = body.model_fields_set  # type: ignore[attr-defined]
    for name in fields:
        if name in supplied and getattr(body, name) is None:
            raise validation_error(name, "cannot be null")


async def _claim(session: AsyncSession, name: str, kind: CatalogKind) -> CatalogName:
    try:
        return await catalog.claim_name(session, name, kind)
    except catalog.NameProblem as exc:
        raise validation_error("name", str(exc)) from exc


async def _rename(session: AsyncSession, name_id: uuid.UUID, new_name: str) -> str:
    try:
        return await catalog.rename(session, name_id, new_name)
    except catalog.NameProblem as exc:
        raise validation_error("name", str(exc)) from exc


async def _delete_catalog_item(
    session: AsyncSession, authz: Any, row: Model | Alias | Router
) -> None:
    """409 while referenced; otherwise delete the row, its name and its (audited) grants."""
    used_by = await catalog.referrers(session, row.id)
    if used_by:
        raise conflict(f"{row.name!r} is referenced by: {', '.join(used_by)}")
    await catalog.audit_grant_removal(session, [(row.id, row.name)], principal=authz.principal)
    await session.delete(row)
    await session.flush()
    await catalog.delete_name(session, row.id)


async def _existing_models(session: AsyncSession, ids: list[uuid.UUID], field_name: str) -> None:
    """Members and fallbacks are models: an alias, a router or an unknown id is a 422."""
    if not ids:
        return
    found = set((await session.scalars(select(Model.name_id).where(Model.name_id.in_(ids)))).all())
    missing = [str(item) for item in ids if item not in found]
    if missing:
        raise validation_error(field_name, f"not models: {', '.join(missing)}")


def _apply_health(row: Upstream, health: HealthCheckIn | None) -> None:
    row.health_check_interval_ms = health.interval_ms if health else None
    row.health_check_timeout_ms = health.timeout_ms if health else None
    row.health_check_unhealthy_threshold = health.unhealthy_threshold if health else None
    row.health_check_healthy_threshold = health.healthy_threshold if health else None


def _sealed_api_key(settings: Settings, api_key: SecretStr) -> bytes:
    try:
        return sealing.seal(settings, api_key.get_secret_value())
    except sealing.SealingUnavailable as exc:
        raise validation_error(
            "api_key", "LUMEWARP_ENCRYPTION_KEY is not configured on the control plane"
        ) from exc


async def _reports(session: AsyncSession, settings: Settings, column) -> list[dict]:
    """The latest health reports of the connected gateways (one column of them)."""
    return list(
        (
            await session.scalars(
                select(column).where(
                    GatewaySession.scope == settings.scope, GatewaySession.connected.is_(True)
                )
            )
        ).all()
    )


def _health(reports: list[dict], name: str) -> str:
    """Unhealthy if any connected gateway says so; unknown before any has reported."""
    seen = [r[name] for r in reports if isinstance(r.get(name), bool)]
    return "unhealthy" if False in seen else "healthy" if seen else "unknown"


_PROBE_CLEARED = {"probe_at": None, "probe_ok": None, "probe_ok_at": None}


def _upstream_probed(row: Upstream) -> tuple:
    """What an upstream's Test now depends on; a change makes its result stale."""
    return (row.base_url, row.health_url, row.api_key_ref, row.api_key_ciphertext)


def _model_probed(row: Model) -> tuple:
    """What a model's Test now depends on; a change makes its result stale."""
    return (row.upstream_id, row.upstream_model, row.model_type, row.default_body)


async def _record_probe(session: AsyncSession, entity, conditions: list, ok: bool) -> None:
    """Keep a Test now result on the saved row it probed. The conditions restate the
    probed settings, so a row changed while the probe ran is left alone; updated_at
    stays, as no setting changed.
    """
    values: dict[str, Any] = {
        "probe_at": func.now(),
        "probe_ok": ok,
        "updated_at": entity.updated_at,
    }
    if ok:
        values["probe_ok_at"] = func.now()
    await session.execute(update(entity).where(*conditions).values(**values))
    await session.commit()


async def _upstreams_out(
    session: AsyncSession, settings: Settings, rows: Sequence[Upstream]
) -> list[UpstreamOut]:
    """UpstreamOut with `health` from the connected gateways' latest reports."""
    reports = await _reports(session, settings, GatewaySession.upstream_health)
    out = []
    for row in rows:
        item = UpstreamOut.model_validate(row)
        if row.health_check_interval_ms is not None:
            item.health = _health(reports, row.name)
        out.append(item)
    return out


async def _models_out(
    session: AsyncSession, settings: Settings, rows: Sequence[Model]
) -> list[ModelOut]:
    """ModelOut with `health` from the connected gateways' chat-check reports."""
    reports = await _reports(session, settings, GatewaySession.model_health)
    out = []
    for row in rows:
        item = ModelOut.model_validate(row)
        if row.chat_check_interval_ms is not None:
            item.health = _health(reports, row.name)
        out.append(item)
    return out


def _apply_model_settings(row: Model, body: ModelCreate | ModelUpdate, *, create: bool) -> None:
    """Limits, default body and reasoning; on PATCH only the fields sent."""
    sent = body.model_fields_set
    for name in ("max_input_tokens", "max_output_tokens", "default_body"):
        if create or name in sent:
            setattr(row, name, getattr(body, name))
    if create or "reasoning" in sent:
        r = body.reasoning
        row.reasoning_mode = r.mode if r else "passthrough"
        row.reasoning_fragments = r.fragments if r else None
        row.reasoning_default_level = r.default_level if r else None
    if create or "chat_check" in sent:
        c = body.chat_check
        row.chat_check_interval_ms = c.interval_ms if c else None
        row.chat_check_timeout_ms = c.timeout_ms if c else None
        row.chat_check_unhealthy_threshold = c.unhealthy_threshold if c else None
        row.chat_check_healthy_threshold = c.healthy_threshold if c else None


def _model_changes(body: ModelUpdate) -> dict[str, Any]:
    """Audit: scalars as sent; the body settings only as "changed" (free-form JSON)."""
    changes = body.model_dump(
        exclude_unset=True, exclude={"default_body", "reasoning"}, mode="json"
    )
    for name in ("default_body", "reasoning"):
        if name in body.model_fields_set:
            changes[f"{name}_changed"] = True
    return changes


def _apply_breaker(row: Upstream, breaker: CircuitBreakerIn | None) -> None:
    row.circuit_breaker_enabled = breaker is not None
    row.circuit_failure_threshold = breaker.failure_threshold if breaker else None
    row.circuit_open_ms = breaker.open_ms if breaker else None


# --------------------------------------------------------------------------- router state


@dataclass
class RouterSpec:
    """The full desired state of a router (create body, or stored state + PATCH)."""

    strategy: str
    members: list[RouterMemberIn]
    params: dict[str, Any] = field(default_factory=dict)
    retry: RetryPolicyIn | None = None
    fallback_model_ids: list[uuid.UUID] = field(default_factory=list)
    traffic_split: list[TrafficSplitIn] = field(default_factory=list)
    sticky: StickyBy = StickyBy.NONE
    sticky_header: str | None = None
    plugin_timeout_ms: int = 0


async def _load_spec(session: AsyncSession, scope: str, row: Router) -> RouterSpec:
    out = await _router_out(session, scope, row)
    return RouterSpec(
        strategy=out.strategy,
        params=out.params,
        members=[RouterMemberIn(**m.model_dump()) for m in out.members],
        retry=RetryPolicyIn(**out.retry.model_dump()) if out.retry else None,
        fallback_model_ids=list(out.fallback_model_ids),
        traffic_split=[TrafficSplitIn(**s.model_dump()) for s in out.traffic_split],
        sticky=row.sticky,
        sticky_header=row.sticky_header,
        plugin_timeout_ms=row.plugin_timeout_ms,
    )


def _members_problem(members: list[RouterMemberIn], field_name: str) -> None:
    for member in members:
        if problem := params_problem(member.params, "member params"):
            raise validation_error(field_name, problem)


async def _validate_spec(session: AsyncSession, spec: RouterSpec) -> None:
    """422 for anything the gateway would reject, and for members that are not models."""
    if problem := router_problem(spec.strategy, spec.params):
        raise validation_error("strategy", problem)
    member_ids = [m.model_id for m in spec.members]
    ensure_unique_ids(member_ids, "members")
    _members_problem(spec.members, "members")
    await _existing_models(session, member_ids, "members")
    for split in spec.traffic_split:
        _members_problem(split.members, "traffic_split")
        await _existing_models(session, [m.model_id for m in split.members], "traffic_split")
    if not spec.traffic_split and not spec.members:
        raise validation_error("members", "pick at least one model or define a traffic_split")
    if spec.traffic_split and spec.members:
        raise validation_error("members", "with a traffic_split, members go under each split")
    # Sticky (snapshot.rs::validate_split).
    if spec.sticky is StickyBy.HEADER and not spec.sticky_header:
        raise validation_error("sticky_header", "required when sticky is 'header'")
    if spec.sticky is not StickyBy.HEADER and spec.sticky_header:
        raise validation_error("sticky_header", "only used when sticky is 'header'")
    if spec.sticky is not StickyBy.NONE and not spec.traffic_split:
        raise validation_error("sticky", "sticky needs a traffic_split")
    # A weighted pool whose members all have weight 0 makes the gateway NACK.
    if spec.strategy == "weighted":
        for split in spec.traffic_split:
            if not sum(m.weight for m in split.members):
                raise validation_error(
                    "traffic_split",
                    f"split {split.name!r} of a weighted router needs a positive total weight",
                )
        if spec.members and not sum(m.weight for m in spec.members):
            raise validation_error("members", "a weighted router needs a positive total weight")
    ensure_unique_ids(spec.fallback_model_ids, "fallback_model_ids")
    # Fallbacks spend the router's attempt budget: with a single attempt they never run.
    if spec.fallback_model_ids and (spec.retry is None or spec.retry.max_attempts == 1):
        raise validation_error(
            "fallback_model_ids", "fallbacks need a retry policy with at least 2 attempts"
        )
    await _existing_models(session, spec.fallback_model_ids, "fallback_model_ids")
    all_ids = (
        member_ids
        + spec.fallback_model_ids
        + [m.model_id for s in spec.traffic_split for m in s.members]
    )
    kinds = set(
        (await session.scalars(select(Model.model_type).where(Model.name_id.in_(all_ids)))).all()
    )
    if len(kinds) > 1:
        raise validation_error(
            "members", "router members, split members and fallbacks must share one model_type"
        )


async def _validate_strategy(
    session: AsyncSession, settings: Any, spec: RouterSpec, *, force: bool
) -> list[str]:
    """One registry, read once. Returns the connected gateways `force` saved past."""
    try:
        return await validate_plugin_params(
            session,
            PluginKind.ROUTER_STRATEGY,
            spec.strategy,
            spec.params,
            members=member_settings(spec.members, spec.traffic_split),
            force=force,
            scope=settings.scope,
        )
    except PluginRejected as exc:
        raise validation_error(exc.field, exc.message) from exc


def _audit_forced(
    session: AsyncSession, authz: Any, row: Router, strategy: str, missing: list[str]
) -> None:
    audit_forced(
        session,
        router_id=row.id,
        name=row.name,
        strategy=strategy,
        missing=missing,
        principal=authz.principal,
    )


async def _write_children(
    session: AsyncSession, row: Router, spec: RouterSpec, fields: set[str]
) -> None:
    """Replace the child rows named in `fields` with `spec`'s lists."""
    if "members" in fields:
        await session.execute(delete(RouterMember).where(RouterMember.router_id == row.id))
        for position, member in enumerate(spec.members):
            session.add(
                RouterMember(
                    router_id=row.id,
                    model_id=member.model_id,
                    weight=member.weight,
                    params=member.params,
                    position=position,
                )
            )
    if "fallback_model_ids" in fields:
        await session.execute(delete(RouterFallback).where(RouterFallback.router_id == row.id))
        for position, model_id in enumerate(spec.fallback_model_ids):
            session.add(RouterFallback(router_id=row.id, model_id=model_id, position=position))
    if "traffic_split" in fields:
        await session.execute(delete(RouterSplit).where(RouterSplit.router_id == row.id))
        await session.flush()
        for position, split in enumerate(spec.traffic_split):
            split_row = RouterSplit(
                router_id=row.id, name=split.name, weight=split.weight, position=position
            )
            session.add(split_row)
            await session.flush()
            for member_position, member in enumerate(split.members):
                session.add(
                    RouterSplitMember(
                        split_id=split_row.id,
                        model_id=member.model_id,
                        weight=member.weight,
                        params=member.params,
                        position=member_position,
                    )
                )


def _apply_scalars(row: Router, spec: RouterSpec) -> None:
    row.strategy = spec.strategy
    row.params = spec.params
    row.retry_enabled = spec.retry is not None
    row.retry_max_attempts = spec.retry.max_attempts if spec.retry else None
    row.retry_on_status = list(spec.retry.retry_on_status) if spec.retry else []
    row.retry_total_timeout_ms = spec.retry.total_timeout_ms if spec.retry else 0
    row.sticky = spec.sticky
    row.sticky_header = spec.sticky_header
    row.plugin_timeout_ms = spec.plugin_timeout_ms


def _router_out_problems(
    registry: PluginRegistry,
    row: Router,
    members: list[RouterMemberOut],
    splits: list[TrafficSplitOut],
) -> list[str]:
    return registry.router_problems(row.strategy, row.params, member_settings(members, splits))


async def _routers_out(session: AsyncSession, scope: str, rows: list[Router]) -> list[RouterOut]:
    ids = [row.id for row in rows]
    registry = await load_plugins(session, scope)
    members: dict[uuid.UUID, list[RouterMemberOut]] = {i: [] for i in ids}
    fallbacks: dict[uuid.UUID, list[uuid.UUID]] = {i: [] for i in ids}
    splits: dict[uuid.UUID, list[TrafficSplitOut]] = {i: [] for i in ids}
    if rows:
        for member in await session.scalars(
            select(RouterMember)
            .where(RouterMember.router_id.in_(ids))
            .order_by(RouterMember.router_id, RouterMember.position)
        ):
            members[member.router_id].append(RouterMemberOut.model_validate(member))
        for edge in await session.scalars(
            select(RouterFallback)
            .where(RouterFallback.router_id.in_(ids))
            .order_by(RouterFallback.router_id, RouterFallback.position)
        ):
            fallbacks[edge.router_id].append(edge.model_id)
        split_rows = (
            await session.scalars(
                select(RouterSplit)
                .where(RouterSplit.router_id.in_(ids))
                .order_by(RouterSplit.router_id, RouterSplit.position)
            )
        ).all()
        split_members: dict[uuid.UUID, list[RouterMemberOut]] = {s.id: [] for s in split_rows}
        if split_rows:
            for member in await session.scalars(
                select(RouterSplitMember)
                .where(RouterSplitMember.split_id.in_(split_members))
                .order_by(RouterSplitMember.split_id, RouterSplitMember.position)
            ):
                split_members[member.split_id].append(RouterMemberOut.model_validate(member))
        for s in split_rows:
            splits[s.router_id].append(
                TrafficSplitOut(name=s.name, weight=s.weight, members=split_members[s.id])
            )
    problems = {
        row.id: _router_out_problems(registry, row, members[row.id], splits[row.id]) for row in rows
    }
    types = await catalog_types(session)
    return [
        RouterOut(
            model_type=types.get(row.id),
            id=row.id,
            name=row.name,
            strategy=row.strategy,
            params=row.params,
            members=members[row.id],
            retry=RetryPolicyOut(
                max_attempts=row.retry_max_attempts,
                retry_on_status=list(row.retry_on_status),
                total_timeout_ms=row.retry_total_timeout_ms,
            )
            if row.retry_enabled
            else None,
            fallback_model_ids=fallbacks[row.id],
            traffic_split=splits[row.id],
            sticky=row.sticky,
            sticky_header=row.sticky_header,
            plugin_timeout_ms=row.plugin_timeout_ms,
            config_invalid=bool(problems[row.id]),
            config_problems=problems[row.id],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def _router_out(session: AsyncSession, scope: str, row: Router) -> RouterOut:
    return (await _routers_out(session, scope, [row]))[0]


# --------------------------------------------------------------------------- upstreams


@router.get("/upstreams", response_model=Page[UpstreamOut], operation_id="listUpstreams")
async def list_upstreams(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[UpstreamOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Upstream).order_by(Upstream.id)
    rows, next_cursor = await uuid_page(session, stmt, Upstream.id, limit, cursor)
    return Page[UpstreamOut](
        items=await _upstreams_out(session, settings, rows), next_cursor=next_cursor
    )


@router.post(
    "/upstreams",
    response_model=UpstreamOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createUpstream",
)
async def create_upstream(
    body: UpstreamCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> UpstreamOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = Upstream(
            name=body.name,
            kind=body.kind,
            base_url=body.base_url,
            api_key_ref=body.api_key_ref,
            api_key_ciphertext=(
                _sealed_api_key(settings, body.api_key) if body.api_key is not None else None
            ),
            timeout_ms=body.timeout_ms,
            stream_usage_mode=body.stream_usage_mode,
            metrics_url=body.metrics_url,
            health_url=body.health_url,
        )
        _apply_health(row, body.health_check)
        _apply_breaker(row, body.circuit_breaker)
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="upstream.create",
            resource_type="upstream",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "base_url": row.base_url,
                "credential_stored": row.api_key_ciphertext is not None,
            },
        )
        await enqueue_recompile(session, settings, "api.upstream.create")
    return (await _upstreams_out(session, settings, [row]))[0]


@router.get("/upstreams/{upstream_id}", response_model=UpstreamOut, operation_id="getUpstream")
async def get_upstream(
    upstream_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> UpstreamOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Upstream, upstream_id)
    if row is None:
        raise not_found("upstream not found")
    return (await _upstreams_out(session, settings, [row]))[0]


@router.get(
    "/upstreams/{upstream_id}/models",
    response_model=UpstreamModelsOut,
    operation_id="listUpstreamModels",
)
async def list_upstream_models(
    upstream_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> UpstreamModelsOut:
    """The upstream's own `GET /models`, called now with its credential. 502 when it
    cannot be reached or does not answer with an OpenAI model list.
    """
    # Uses the upstream's credential, so it needs the right to change the upstream.
    await authz.require(Action.ROUTING_MANAGE)
    row = await session.get(Upstream, upstream_id)
    if row is None:
        raise not_found("upstream not found")
    try:
        models = await upstream_client.list_models(settings, row)
    except upstream_client.UpstreamCallFailed as exc:
        raise ApiError(502, "upstream_unavailable", str(exc)) from None
    return UpstreamModelsOut(models=[UpstreamModelInfoOut(**vars(m)) for m in models])


@router.post("/upstreams/test", response_model=UpstreamTestOut, operation_id="testUpstream")
async def test_upstream(
    body: UpstreamTestIn, authz: Authz, session: SessionDep, settings: SettingsDep
) -> UpstreamTestOut:
    """Probe an upstream once now from the control plane, as the form stands (saved or
    not): its health URL (configured or base_url + /models). Reflects the control
    plane's network, not each gateway's. A key typed in the form is used once and never
    stored.
    """
    await authz.require(Action.ROUTING_MANAGE)
    token: str | None = None
    credential = "none"
    if body.api_key is not None:
        token, credential = body.api_key.get_secret_value(), "entered"
    elif body.api_key_ref is not None:
        token = os.environ.get(body.api_key_ref) or None
        credential = "env" if token else "env_unset"
    elif body.upstream_id is not None:
        row = await session.get(Upstream, body.upstream_id)
        if row is None:
            raise not_found("upstream not found")
        if row.api_key_ciphertext is not None:
            try:
                token = upstream_client.bearer(settings, row)
            except upstream_client.UpstreamCallFailed as exc:
                raise ApiError(502, "upstream_unavailable", str(exc)) from None
            credential = "stored"
    probe = await upstream_client.test_upstream(body.base_url, body.health_url, token)
    if body.upstream_id is not None and body.api_key is None:
        await _record_probe(
            session,
            Upstream,
            [
                Upstream.id == body.upstream_id,
                Upstream.base_url == body.base_url,
                Upstream.health_url.is_not_distinct_from(body.health_url),
                Upstream.api_key_ref.is_not_distinct_from(body.api_key_ref),
            ],
            probe.ok,
        )
    return UpstreamTestOut(credential=credential, probe=ProbeOut(**vars(probe)))


@router.patch("/upstreams/{upstream_id}", response_model=UpstreamOut, operation_id="updateUpstream")
async def update_upstream(
    upstream_id: uuid.UUID,
    body: UpstreamUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> UpstreamOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(body, "name", "kind", "base_url", "timeout_ms", "stream_usage_mode")
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Upstream, upstream_id)
        if row is None:
            raise not_found("upstream not found")
        probed = _upstream_probed(row)
        for name in ("name", "kind", "base_url", "timeout_ms", "stream_usage_mode"):
            if name in body.model_fields_set:
                setattr(row, name, getattr(body, name))
        if "api_key_ref" in body.model_fields_set:
            row.api_key_ref = body.api_key_ref
            if body.api_key_ref is not None:
                row.api_key_ciphertext = None
        if "api_key" in body.model_fields_set:
            row.api_key_ciphertext = (
                _sealed_api_key(settings, body.api_key) if body.api_key is not None else None
            )
            if body.api_key is not None:
                row.api_key_ref = None
        if "health_url" in body.model_fields_set:
            row.health_url = body.health_url
        if "metrics_url" in body.model_fields_set:
            row.metrics_url = body.metrics_url
        if "health_check" in body.model_fields_set:
            _apply_health(row, body.health_check)
        if "circuit_breaker" in body.model_fields_set:
            _apply_breaker(row, body.circuit_breaker)
        if _upstream_probed(row) != probed:
            for name, value in _PROBE_CLEARED.items():
                setattr(row, name, value)
            # Its models' results went through the old address or key.
            await session.execute(
                update(Model)
                .where(Model.upstream_id == row.id)
                .values(**_PROBE_CLEARED, updated_at=Model.updated_at)
            )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="upstream.update",
            resource_type="upstream",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "changes": body.model_dump(exclude_unset=True, exclude={"api_key"}, mode="json"),
                "credential_changed": "api_key" in body.model_fields_set,
            },
        )
        await enqueue_recompile(session, settings, "api.upstream.update")
    return (await _upstreams_out(session, settings, [row]))[0]


@router.delete(
    "/upstreams/{upstream_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteUpstream",
)
async def delete_upstream(
    upstream_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Upstream, upstream_id)
        if row is None:
            raise not_found("upstream not found")
        model = await session.scalar(select(Model.id).where(Model.upstream_id == row.id).limit(1))
        if model is not None:
            raise conflict("upstream still has models")
        await session.delete(row)
        audit.record(
            session,
            action="upstream.delete",
            resource_type="upstream",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.upstream.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- catalog


@router.get("/catalog", response_model=Page[CatalogEntryOut], operation_id="listCatalog")
async def list_catalog(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[CatalogEntryOut]:
    # Names and kinds only: any authenticated caller needs them to pick a team allowlist.
    await authz.require(Action.CATALOG_READ)
    stmt = select(CatalogName).order_by(CatalogName.id)
    rows, next_cursor = await uuid_page(session, stmt, CatalogName.id, limit, cursor)
    types = await catalog_types(session)
    return Page[CatalogEntryOut](
        items=[
            CatalogEntryOut.model_validate(row).model_copy(update={"model_type": types.get(row.id)})
            for row in rows
        ],
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- models


@router.get("/models", response_model=Page[ModelOut], operation_id="listModels")
async def list_models(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[ModelOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Model).order_by(Model.name_id)
    rows, next_cursor = await uuid_page(session, stmt, Model.name_id, limit, cursor)
    return Page[ModelOut](
        items=await _models_out(session, settings, rows),
        next_cursor=next_cursor,
    )


@router.post(
    "/models",
    response_model=ModelOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createModel",
)
async def create_model(
    body: ModelCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> ModelOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        upstream = await session.get(Upstream, body.upstream_id)
        if upstream is None:
            raise validation_error("upstream_id", "unknown upstream")
        name = await _claim(session, body.name, CatalogKind.MODEL)
        row = Model(
            name_id=name.id,
            upstream_id=upstream.id,
            upstream_model=body.upstream_model,
            model_type=body.model_type,
            price_nano_usd_per_audio_second=body.price_nano_usd_per_audio_second,
            price_input_nano_usd_per_mtok=body.price_input_nano_usd_per_mtok,
            price_output_nano_usd_per_mtok=body.price_output_nano_usd_per_mtok,
        )
        _apply_model_settings(row, body, create=True)
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="model.create",
            resource_type="model",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "upstream": upstream.name, "model_type": row.model_type},
        )
        await enqueue_recompile(session, settings, "api.model.create")
    return (await _models_out(session, settings, [row]))[0]


@router.get(
    "/models/{model_id}",
    response_model=ModelOut,
    operation_id="getModel",
)
async def get_model(
    model_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> ModelOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Model, model_id)
    if row is None:
        raise not_found("model not found")
    return (await _models_out(session, settings, [row]))[0]


@router.post("/models/test", response_model=ModelTestOut, operation_id="testModel")
async def test_model(
    body: ModelTestIn, authz: Authz, session: SessionDep, settings: SettingsDep
) -> ModelTestOut:
    """Probe the model's endpoint from the control plane with the form's current type
    and settings (saved or not). Uses the upstream's credential and JSON defaults.
    Reflects the control plane's network, not each gateway's.
    """
    await authz.require(Action.ROUTING_MANAGE)
    upstream = await session.get(Upstream, body.upstream_id)
    if upstream is None:
        raise validation_error("upstream_id", "unknown upstream")
    credential = "none"
    try:
        token = upstream_client.bearer(settings, upstream)
    except upstream_client.UpstreamCallFailed as exc:
        raise ApiError(502, "upstream_unavailable", str(exc)) from None
    if upstream.api_key_ciphertext is not None:
        credential = "stored"
    elif upstream.api_key_ref:
        credential = "env" if token else "env_unset"
    probe, reply = await upstream_client.chat_check(
        upstream.base_url, token, body.upstream_model, body.default_body, body.model_type
    )
    if body.model_id is not None:
        await _record_probe(
            session,
            Model,
            [
                Model.name_id == body.model_id,
                Model.upstream_id == body.upstream_id,
                Model.upstream_model == body.upstream_model,
                Model.model_type == body.model_type,
                Model.default_body.is_not_distinct_from(body.default_body),
            ],
            probe.ok,
        )
    return ModelTestOut(credential=credential, probe=ProbeOut(**vars(probe)), reply=reply)


@router.patch(
    "/models/{model_id}",
    response_model=ModelOut,
    operation_id="updateModel",
)
async def update_model(
    model_id: uuid.UUID,
    body: ModelUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> ModelOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(body, "name", "upstream_id", "upstream_model", "model_type")
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Model, model_id)
        if row is None:
            raise not_found("model not found")
        probed = _model_probed(row)
        if body.model_type is not None and body.model_type != row.model_type:
            for edge in (RouterMember, RouterSplitMember, RouterFallback):
                if await session.scalar(
                    select(edge.model_id).where(edge.model_id == row.id).limit(1)
                ):
                    raise ApiError(409, "model_type_in_use", "model type is in use by a router")
        renaming = body.name is not None and body.name != row.name
        if body.keep_old_name_as_alias and not renaming:
            raise validation_error("keep_old_name_as_alias", "needs a new name")
        if body.upstream_id is not None:
            upstream = await session.get(Upstream, body.upstream_id)
            if upstream is None:
                raise validation_error("upstream_id", "unknown upstream")
            row.upstream_id = upstream.id
        if body.upstream_model is not None:
            row.upstream_model = body.upstream_model
        # Finish all lookups before changing type-dependent fields: a query could
        # otherwise autoflush a temporarily inconsistent state during a PATCH.
        if body.model_type is not None:
            row.model_type = body.model_type
        if "price_nano_usd_per_audio_second" in body.model_fields_set:
            row.price_nano_usd_per_audio_second = body.price_nano_usd_per_audio_second
        price_in = (
            body.price_input_nano_usd_per_mtok
            if "price_input_nano_usd_per_mtok" in body.model_fields_set
            else row.price_input_nano_usd_per_mtok
        )
        price_out = (
            body.price_output_nano_usd_per_mtok
            if "price_output_nano_usd_per_mtok" in body.model_fields_set
            else row.price_output_nano_usd_per_mtok
        )
        row.price_input_nano_usd_per_mtok = price_in
        row.price_output_nano_usd_per_mtok = price_out
        _apply_model_settings(row, body, create=False)
        try:
            validate_model_settings(row)
        except ValueError as exc:
            raise validation_error("model_type", str(exc)) from None
        if _model_probed(row) != probed:
            for name, value in _PROBE_CLEARED.items():
                setattr(row, name, value)
        if renaming:
            old_name = await _rename(session, row.id, body.name)
            row.updated_at = func.now()
            audit.record(
                session,
                action="model.rename",
                resource_type="model",
                resource_id=row.id,
                principal=authz.principal,
                details={"old_name": old_name, "new_name": body.name},
            )
            if body.keep_old_name_as_alias:
                name = await _claim(session, old_name, CatalogKind.ALIAS)
                session.add(
                    Alias(
                        name_id=name.id,
                        target_name_id=row.id,
                        target_kind=CatalogKind.MODEL.value,
                    )
                )
                await session.flush()
                copied = await catalog.copy_grants(session, row.id, name.id)
                audit.record(
                    session,
                    action="alias.create",
                    resource_type="alias",
                    resource_id=name.id,
                    principal=authz.principal,
                    details={
                        "name": old_name,
                        "target": row.id,
                        "kept_from_rename": True,
                        "grants_copied": copied,
                    },
                )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="model.update",
            resource_type="model",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "changes": _model_changes(body)},
        )
        await enqueue_recompile(session, settings, "api.model.update")
    return (await _models_out(session, settings, [row]))[0]


@router.delete(
    "/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteModel",
)
async def delete_model(
    model_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Model, model_id)
        if row is None:
            raise not_found("model not found")
        await _delete_catalog_item(session, authz, row)
        audit.record(
            session,
            action="model.delete",
            resource_type="model",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.model.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- aliases


async def _alias_target(session: AsyncSession, target: uuid.UUID) -> CatalogKind:
    kind = await session.scalar(select(CatalogName.kind).where(CatalogName.id == target))
    if kind is None:
        raise validation_error("target", "unknown model or router")
    if kind is CatalogKind.ALIAS:
        raise validation_error("target", "an alias cannot point at another alias")
    return kind


async def _alias_out(session: AsyncSession, row: Alias, types=None) -> AliasOut:
    item = AliasOut.model_validate(row)
    item.model_type = (await catalog_types(session) if types is None else types).get(row.id)
    return item


@router.get("/aliases", response_model=Page[AliasOut], operation_id="listAliases")
async def list_aliases(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[AliasOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Alias).order_by(Alias.name_id)
    rows, next_cursor = await uuid_page(session, stmt, Alias.name_id, limit, cursor)
    types = await catalog_types(session)
    return Page[AliasOut](
        items=[await _alias_out(session, row, types) for row in rows], next_cursor=next_cursor
    )


@router.post(
    "/aliases",
    response_model=AliasOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createAlias",
)
async def create_alias(
    body: AliasCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> AliasOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        kind = await _alias_target(session, body.target)
        name = await _claim(session, body.name, CatalogKind.ALIAS)
        row = Alias(name_id=name.id, target_name_id=body.target, target_kind=kind.value)
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="alias.create",
            resource_type="alias",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "target": body.target},
        )
        await enqueue_recompile(session, settings, "api.alias.create")
    return await _alias_out(session, row)


@router.get("/aliases/{alias_id}", response_model=AliasOut, operation_id="getAlias")
async def get_alias(alias_id: uuid.UUID, authz: Authz, session: SessionDep) -> AliasOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Alias, alias_id)
    if row is None:
        raise not_found("alias not found")
    return await _alias_out(session, row)


@router.patch("/aliases/{alias_id}", response_model=AliasOut, operation_id="updateAlias")
async def update_alias(
    alias_id: uuid.UUID,
    body: AliasUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> AliasOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(body, "name", "target")
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Alias, alias_id)
        if row is None:
            raise not_found("alias not found")
        if body.target is not None:
            kind = await _alias_target(session, body.target)
            row.target_name_id = body.target
            row.target_kind = kind.value
        if body.name is not None and body.name != row.name:
            old_name = await _rename(session, row.id, body.name)
            row.updated_at = func.now()
            audit.record(
                session,
                action="alias.rename",
                resource_type="alias",
                resource_id=row.id,
                principal=authz.principal,
                details={"old_name": old_name, "new_name": body.name},
            )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="alias.update",
            resource_type="alias",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "changes": body.model_dump(exclude_unset=True, mode="json")},
        )
        await enqueue_recompile(session, settings, "api.alias.update")
    return await _alias_out(session, row)


@router.delete(
    "/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteAlias",
)
async def delete_alias(
    alias_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Alias, alias_id)
        if row is None:
            raise not_found("alias not found")
        await _delete_catalog_item(session, authz, row)
        audit.record(
            session,
            action="alias.delete",
            resource_type="alias",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.alias.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- routers


@router.get("/routers", response_model=Page[RouterOut], operation_id="listRouters")
async def list_routers(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[RouterOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Router).order_by(Router.name_id)
    rows, next_cursor = await uuid_page(session, stmt, Router.name_id, limit, cursor)
    return Page[RouterOut](
        items=await _routers_out(session, settings.scope, rows), next_cursor=next_cursor
    )


@router.post(
    "/routers",
    response_model=RouterOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createRouter",
)
async def create_router(
    body: RouterCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> RouterOut:
    await authz.require(Action.ROUTING_MANAGE)
    spec = RouterSpec(
        strategy=body.strategy,
        params=body.params,
        members=list(body.members),
        retry=body.retry,
        fallback_model_ids=list(body.fallback_model_ids),
        traffic_split=list(body.traffic_split),
        sticky=body.sticky,
        sticky_header=body.sticky_header,
        plugin_timeout_ms=body.plugin_timeout_ms,
    )
    async with mutation(session):
        await lock_scope(session, settings)
        await _validate_spec(session, spec)
        forced = await _validate_strategy(session, settings, spec, force=body.force)
        name = await _claim(session, body.name, CatalogKind.ROUTER)
        row = Router(name_id=name.id)
        _apply_scalars(row, spec)
        session.add(row)
        await session.flush()
        await _write_children(
            session, row, spec, {"members", "fallback_model_ids", "traffic_split"}
        )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="router.create",
            resource_type="router",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "members": [str(m.model_id) for m in spec.members],
                "settings": body.model_dump(
                    mode="json",
                    exclude={
                        "name": True,
                        "force": True,
                        "params": True,
                        "members": True,
                        "traffic_split": {"__all__": {"members": {"__all__": {"params"}}}},
                    },
                    exclude_defaults=True,
                ),
            },
        )
        _audit_forced(session, authz, row, spec.strategy, forced)
        await enqueue_recompile(session, settings, "api.router.create")
    return await _router_out(session, settings.scope, row)


@router.get("/routers/{router_id}", response_model=RouterOut, operation_id="getRouter")
async def get_router(
    router_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> RouterOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Router, router_id)
    if row is None:
        raise not_found("router not found")
    return await _router_out(session, settings.scope, row)


@router.patch("/routers/{router_id}", response_model=RouterOut, operation_id="updateRouter")
async def update_router(
    router_id: uuid.UUID,
    body: RouterUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> RouterOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(
        body,
        "name",
        "strategy",
        "members",
        "fallback_model_ids",
        "traffic_split",
        "sticky",
        "params",
    )
    fields = body.model_fields_set
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Router, router_id)
        if row is None:
            raise not_found("router not found")
        spec = await _load_spec(session, settings.scope, row)
        for name in fields - {"name", "force"}:
            setattr(spec, name, getattr(body, name))
        # Params belong to one strategy: changing it drops them unless the body sends new ones.
        if "strategy" in fields and "params" not in fields and body.strategy != row.strategy:
            spec.params = {}
        # Leaving header stickiness drops the header unless the body names one.
        if spec.sticky is not StickyBy.HEADER and "sticky_header" not in fields:
            spec.sticky_header = None
        await _validate_spec(session, spec)
        # Settings already stored stay as they are (`config_invalid`) until they are changed:
        # an upgraded schema must not block a rename or a retry change.
        forced = []
        if fields & {"strategy", "params", "members", "traffic_split"}:
            forced = await _validate_strategy(session, settings, spec, force=body.force)
        if body.name is not None and body.name != row.name:
            old_name = await _rename(session, row.id, body.name)
            row.updated_at = func.now()
            audit.record(
                session,
                action="router.rename",
                resource_type="router",
                resource_id=row.id,
                principal=authz.principal,
                details={"old_name": old_name, "new_name": body.name},
            )
        _apply_scalars(row, spec)
        await _write_children(session, row, spec, fields)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="router.update",
            resource_type="router",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "changes": body.model_dump(
                    exclude_unset=True,
                    mode="json",
                    exclude={
                        "force": True,
                        "params": True,
                        "members": {"__all__": {"params"}},
                        "traffic_split": {"__all__": {"members": {"__all__": {"params"}}}},
                    },
                ),
            },
        )
        _audit_forced(session, authz, row, spec.strategy, forced)
        await enqueue_recompile(session, settings, "api.router.update")
    return await _router_out(session, settings.scope, row)


@router.delete(
    "/routers/{router_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteRouter",
)
async def delete_router(
    router_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Router, router_id)
        if row is None:
            raise not_found("router not found")
        await _delete_catalog_item(session, authz, row)
        audit.record(
            session,
            action="router.delete",
            resource_type="router",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.router.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
