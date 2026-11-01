"""compile_scope: source state -> one resolved Snapshot (not yet versioned)."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.strategy import params_json
from lumewarp_control_plane.compiler.validate import (
    RESERVED_ALIASES,
    SnapshotInvalid,
    capabilities_used,
    metrics_url_problem,
    upstream_url_problem,
    validate_snapshot,
)
from lumewarp_control_plane.models import (
    ApiKey,
    GatewaySession,
    KeyLifecycle,
    KeyOwnerKind,
    Model,
    ModelAccess,
    Router,
    RouterFallback,
    RouterModel,
    RouterSplit,
    RouterSplitModel,
    RoutingStrategy,
    Scope,
    ScopeType,
    ServiceAccount,
    StickyBy,
    StreamUsageMode,
    Team,
    TeamMember,
    Upstream,
    UpstreamKind,
    UsageTotal,
    User,
)
from lumewarp_control_plane.settings import get_settings
from lumewarp_control_plane.windows import month_window

log = logging.getLogger(__name__)

UPSTREAM_KIND = {
    UpstreamKind.OPENAI: pb.UPSTREAM_KIND_OPENAI,
    UpstreamKind.VLLM: pb.UPSTREAM_KIND_VLLM,
    UpstreamKind.OPENAI_COMPATIBLE: pb.UPSTREAM_KIND_OPENAI_COMPATIBLE,
}
STREAM_USAGE_MODE = {
    StreamUsageMode.INCLUDE_USAGE: pb.STREAM_USAGE_MODE_INCLUDE_USAGE,
    StreamUsageMode.PASSTHROUGH: pb.STREAM_USAGE_MODE_PASSTHROUGH,
}
ROUTING_STRATEGY = {
    RoutingStrategy.WEIGHTED: pb.ROUTING_STRATEGY_WEIGHTED,
    RoutingStrategy.LEAST_IN_FLIGHT: pb.ROUTING_STRATEGY_LEAST_IN_FLIGHT,
    RoutingStrategy.LOWEST_LATENCY: pb.ROUTING_STRATEGY_LOWEST_LATENCY,
    RoutingStrategy.VLLM_LOAD: pb.ROUTING_STRATEGY_VLLM_LOAD,
    RoutingStrategy.CUSTOM: pb.ROUTING_STRATEGY_CUSTOM,
}
STICKY_BY = {
    StickyBy.NONE: pb.STICKY_BY_NONE,
    StickyBy.HEADER: pb.STICKY_BY_HEADER,
    StickyBy.TEAM: pb.STICKY_BY_TEAM,
    StickyBy.KEY: pb.STICKY_BY_KEY,
}


class CompileError(Exception):
    """Source state cannot be compiled into a valid snapshot (e.g. over the snapshot
    size/key limits). Nothing is published; the outbox row is marked failed with this
    message, which must not contain secrets.
    """


@dataclass(frozen=True)
class CompiledSnapshot:
    """Result of `compile_scope`, not yet published.

    `snapshot.version` and `created_at_unix_ms` are 0 here; publishing assigns them.
    `warnings` lists source entities left out of the snapshot (e.g. an empty router).
    """

    snapshot: pb.Snapshot
    source_revision: int
    key_count: int
    warnings: tuple[str, ...] = field(default=())


# Effective name set: None = every name (universal), else exactly these aliases.
ModelSet = frozenset[str] | None


def narrow(parent: ModelSet, mode: ModelAccess, allowed: Sequence[str]) -> ModelSet:
    """One level of allowlist inheritance. INHERIT and ALL keep the parent's set (ALL on
    a team is the universal set); LIST intersects. An empty LIST is deny-all.
    """
    if mode is not ModelAccess.LIST:
        return parent
    own = frozenset(m for m in allowed if m)
    return own if parent is None else parent & own


# Rate-limit scope id prefix per level (the wire ids are "team:<id>", "member:<id>",
# "sa:<id>").
SCOPE_PREFIX = {
    ScopeType.TEAM: "team",
    ScopeType.MEMBER: "member",
    ScopeType.SERVICE_ACCOUNT: "sa",
}


def rate_limit_scope(level: ScopeType, scope_id: uuid.UUID, entity) -> pb.RateLimitScope | None:
    if entity.rpm_limit is None and entity.tpm_limit is None and entity.max_parallel is None:
        return None
    return pb.RateLimitScope(
        id=f"{SCOPE_PREFIX[level]}:{scope_id}",
        limit=pb.RateLimit(
            requests_per_minute=entity.rpm_limit or 0,
            tokens_per_minute=entity.tpm_limit or 0,
            max_parallel_requests=entity.max_parallel or 0,
        ),
    )


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_MS = timedelta(milliseconds=1)


def _ms(ts: datetime) -> int:
    """Exact Unix milliseconds (floored), without float rounding."""
    return (ts - EPOCH) // ONE_MS


def expiry_ms(expires_at: datetime | None) -> int:
    """Wire expiry. 0 is the "never expires" sentinel, so only NULL maps to it; any
    expiry at or before the epoch (including sub-millisecond ones that floor to 0) becomes
    1 ms, which every gateway treats as long expired. Never negative (a gateway NACKs that).
    """
    if expires_at is None:
        return 0
    return max(_ms(expires_at), 1)


async def compile_scope(
    session: AsyncSession,
    scope: str,
    *,
    now: datetime | None = None,
    max_snapshot_bytes: int | None = None,
    max_snapshot_keys: int | None = None,
    require_local_membership: bool,
) -> CompiledSnapshot:
    """Compile the current source state of `scope` into a snapshot.

    Pure over source state read in `session` (plus `now` for the budget window);
    the caller runs it in one REPEATABLE READ transaction, or holds the
    scope row lock, so every table is read at one consistent point and `source_revision`
    is `scopes.source_revision` as seen there.

    Contract:
    - Expands team -> owner (member or service account) policy into each key's
      EffectivePolicy. Model access: resolve INHERIT first (the owner takes the team's
      effective set unchanged; teams are never INHERIT), then intersect: ALL is the
      universal set, LIST is exactly `allowed_models`. `allow_all_models` is true iff the
      resulting effective set is still universal, e.g. ALL -> INHERIT (the defaults)
      allows every name. A LIST with an empty list on either level is deny-all
      (`allow_all_models=false`, `allowed_models=[]`), never "all".
      Rate limits: one RateLimitScope per level that sets a limit ("team:<id>",
      "member:<member_scope_id>", "sa:<id>").
      Status: SUSPENDED from lifecycle, or for a user-owned key whose owner is no longer
      a member of the key's team (builtin only) or is deactivated; external owners without
      a local policy row inherit the team policy. Else BUDGET_EXCEEDED if the team or
      the owner has a budget and its `usage_totals` for the UTC month of `now` is >= it;
      else ACTIVE. Revoked keys are left out (the gateway answers 401 for them either
      way), so revocation is a key removal in deltas. Expiry is passed through as
      `expires_at_unix_ms` and enforced by the gateway.
    - Source rows the gateway would reject are left out and reported in `warnings`:
      upstreams with an invalid base URL (and their models), models with an
      empty upstream_model, and routers that end up without models (or with an
      empty traffic split), use a reserved alias, or are weighted with zero total weight
      (per split when split).
      Expiry: NULL -> 0 (never); at or before the epoch -> 1 ms (expired).
    - P2 routing: upstream health_check / circuit_breaker / metrics_url and router retry /
      fallbacks / traffic_split / sticky are emitted when set; `required_capabilities`
      lists exactly the capabilities those fields need (validate.capabilities_used, the
      gateway's rule). A retry policy with max_attempts NULL is emitted as 0, which the
      gateway reads as its default (2). Fallbacks to routers that are left out are
      dropped (warning); a split whose models are all left out leaves the router out.
    - The result is checked against every gateway invariant (`validate.validate_snapshot`,
      mirroring snapshot.rs::validate and RoutingTable::compile); a violation is a CompileError.
    - Enforces settings.max_snapshot_bytes / max_snapshot_keys (CompileError).
    - Deterministic: same source state -> byte-identical serialization (sorted entities).
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    max_bytes = max_snapshot_bytes or settings.max_snapshot_bytes
    max_keys = max_snapshot_keys or settings.max_snapshot_keys
    warnings: list[str] = []

    revision = await session.scalar(select(Scope.source_revision).where(Scope.name == scope))
    if revision is None:
        raise CompileError(f"unknown config scope {scope!r}")

    # Routing.
    upstreams = (await session.scalars(select(Upstream).order_by(Upstream.name))).all()
    upstream_name = {u.id: u.name for u in upstreams}
    models = (await session.scalars(select(Model).order_by(Model.name))).all()
    model_by_id = {d.id: d for d in models}
    routers = (await session.scalars(select(Router).order_by(Router.name))).all()
    advertised = {
        cap
        for caps in await session.scalars(
            select(GatewaySession.capabilities).where(
                GatewaySession.scope == scope, GatewaySession.connected.is_(True)
            )
        )
        for cap in caps
    }
    router_name = {r.id: r.name for r in routers}
    members: dict[uuid.UUID, list[uuid.UUID]] = {}
    for rd in await session.scalars(
        select(RouterModel).order_by(RouterModel.router_id, RouterModel.position)
    ):
        members.setdefault(rd.router_id, []).append(rd.model_id)
    fallbacks: dict[uuid.UUID, list[uuid.UUID]] = {}
    for rf in await session.scalars(
        select(RouterFallback).order_by(RouterFallback.router_id, RouterFallback.position)
    ):
        fallbacks.setdefault(rf.router_id, []).append(rf.fallback_router_id)
    split_rows = (
        await session.scalars(
            select(RouterSplit).order_by(RouterSplit.router_id, RouterSplit.position)
        )
    ).all()
    split_members: dict[uuid.UUID, list[uuid.UUID]] = {}
    for sd in await session.scalars(
        select(RouterSplitModel).order_by(RouterSplitModel.split_id, RouterSplitModel.position)
    ):
        split_members.setdefault(sd.split_id, []).append(sd.model_id)
    splits: dict[uuid.UUID, list[RouterSplit]] = {}
    for sr in split_rows:
        splits.setdefault(sr.router_id, []).append(sr)

    snap = pb.Snapshot(scope=scope)
    # Source entities the gateway would reject are left out (with a warning) instead of
    # failing the compile, so one bad routing row never blocks revocations or budget
    # blocks for the whole fleet. The API should already refuse them (422).
    valid_upstreams: set[uuid.UUID] = set()
    for u in upstreams:
        if (problem := upstream_url_problem(u.base_url)) is not None:
            warnings.append(f"upstream {u.name!r} left out: base_url {problem}")
            continue
        if not u.name:
            warnings.append("upstream with an empty name left out")
            continue
        valid_upstreams.add(u.id)
        up = pb.Upstream(
            name=u.name,
            kind=UPSTREAM_KIND[u.kind],
            base_url=u.base_url,
            api_key_ref=u.api_key_ref or "",
            timeout_ms=u.timeout_ms,
            stream_usage_mode=STREAM_USAGE_MODE[u.stream_usage_mode],
        )
        if u.health_check_interval_ms is not None:
            up.health_check.CopyFrom(
                pb.HealthCheck(
                    path=u.health_check_path or "",
                    interval_ms=u.health_check_interval_ms,
                    timeout_ms=u.health_check_timeout_ms or 0,
                    unhealthy_threshold=u.health_check_unhealthy_threshold or 0,
                    healthy_threshold=u.health_check_healthy_threshold or 0,
                )
            )
        if u.circuit_breaker_enabled:
            up.circuit_breaker.CopyFrom(
                pb.CircuitBreaker(
                    failure_threshold=u.circuit_failure_threshold or 0,
                    open_ms=u.circuit_open_ms or 0,
                )
            )
        if u.metrics_url:
            if (problem := metrics_url_problem(u.metrics_url)) is not None:
                warnings.append(f"upstream {u.name!r}: metrics_url {problem}, left out")
            else:
                up.metrics_url = u.metrics_url
        snap.upstreams.append(up)
    valid_models = []
    for d in models:
        if d.upstream_id not in valid_upstreams:
            warnings.append(f"model {d.name!r} left out: its upstream is left out")
        elif not d.name or not d.upstream_model:
            warnings.append(f"model {d.name!r} left out: empty name or upstream_model")
        else:
            valid_models.append(d)
    valid_model_ids = {d.id for d in valid_models}

    def positive(ids: list[uuid.UUID]) -> bool:
        return sum(model_by_id[i].weight for i in ids) > 0

    # First pass: which routers are emitted (fallbacks may only name emitted routers).
    emitted: list[tuple[Router, pb.Router]] = []
    for r in routers:
        member_ids = members.get(r.id, [])
        dropped = [model_by_id[i].name for i in member_ids if i not in valid_model_ids]
        member_ids = [i for i in member_ids if i in valid_model_ids]
        if dropped:
            warnings.append(f"router {r.name!r}: left-out models {dropped} removed")
        if not r.name or r.name in RESERVED_ALIASES:
            warnings.append(f"router {r.name!r} left out: alias is empty or reserved")
            continue
        weighted = r.strategy is RoutingStrategy.WEIGHTED
        router = pb.Router(name=r.name, strategy=ROUTING_STRATEGY[r.strategy])
        if r.strategy is RoutingStrategy.CUSTOM:
            router.custom_strategy = r.custom_strategy
            router.strategy_params_json = params_json(r.strategy_params or {})
        router_splits = splits.get(r.id, [])
        if router_splits:
            bad = None
            for sr in router_splits:
                ids = [i for i in split_members.get(sr.id, []) if i in valid_model_ids]
                if not ids:
                    bad = f"split {sr.name!r} has no models"
                elif weighted and not positive(ids):
                    bad = f"split {sr.name!r} is weighted with zero total weight"
                if bad:
                    break
                router.traffic_split.append(
                    pb.TrafficSplit(
                        name=sr.name,
                        weight=sr.weight,
                        models=[model_by_id[i].name for i in ids],
                    )
                )
            if bad is None and not sum(sr.weight for sr in router_splits):
                bad = "traffic split weights sum to 0"
            if bad is not None:
                warnings.append(f"router {r.name!r} left out: {bad}")
                continue
            router.sticky = STICKY_BY[r.sticky]
            if r.sticky is StickyBy.HEADER:
                router.sticky_header = r.sticky_header or ""
        else:
            if not member_ids:
                warnings.append(f"router {r.name!r} left out: no models")
                continue
            if weighted and not positive(member_ids):
                warnings.append(f"router {r.name!r} left out: weighted with zero total weight")
                continue
            if r.sticky is not StickyBy.NONE:
                warnings.append(f"router {r.name!r}: sticky without a traffic split ignored")
        router.models.extend(model_by_id[i].name for i in member_ids)
        if r.retry_enabled:
            router.retry.CopyFrom(
                pb.RetryPolicy(
                    # 0 = the gateway default (2 attempts).
                    max_attempts=r.retry_max_attempts or 0,
                    retry_on_status=sorted(set(r.retry_on_status)),
                    total_timeout_ms=r.retry_total_timeout_ms,
                )
            )
        emitted.append((r, router))

    emitted_ids = {r.id for r, _ in emitted}
    for r, router in emitted:
        for fallback in fallbacks.get(r.id, []):
            if fallback in emitted_ids and fallback != r.id:
                router.fallbacks.append(router_name[fallback])
            else:
                warnings.append(
                    f"router {r.name!r}: fallback {router_name.get(fallback)!r} is left out,"
                    " dropped"
                )
        snap.routers.append(router)
        if router.strategy == pb.ROUTING_STRATEGY_CUSTOM:
            capability = f"strategy:{router.custom_strategy}"
            if capability not in advertised:
                warnings.append(
                    f"router {r.name!r}: {capability} is not advertised by any connected gateway"
                )

    for d in valid_models:
        dep = pb.Model(
            name=d.name,
            upstream=upstream_name[d.upstream_id],
            upstream_model=d.upstream_model,
            weight=d.weight,
        )
        if d.price_input_nano_usd_per_mtok is not None:
            dep.price.CopyFrom(
                pb.Price(
                    input_nano_usd_per_mtok=d.price_input_nano_usd_per_mtok,
                    output_nano_usd_per_mtok=d.price_output_nano_usd_per_mtok or 0,
                )
            )
        snap.models.append(dep)

    # Teams, key owners and keys.
    teams = {t.id: t for t in await session.scalars(select(Team))}
    members = {(m.team_id, m.user_id): m for m in await session.scalars(select(TeamMember))}
    service_accounts = {a.id: a for a in await session.scalars(select(ServiceAccount))}
    active_users = set(await session.scalars(select(User.id).where(User.is_active.is_(True))))
    keys = (
        await session.scalars(
            select(ApiKey).where(ApiKey.lifecycle != KeyLifecycle.REVOKED).order_by(ApiKey.id)
        )
    ).all()
    if len(keys) > max_keys:
        raise CompileError(f"{len(keys)} keys exceed the limit of {max_keys}")

    window_start, window_end = month_window(now)
    spent = {
        (row.scope_type, row.scope_id): row.cost_nano_usd
        for row in await session.execute(
            select(UsageTotal.scope_type, UsageTotal.scope_id, UsageTotal.cost_nano_usd).where(
                UsageTotal.window_start == window_start
            )
        )
    }

    def over_budget(level: ScopeType, scope_id: uuid.UUID, entity) -> bool:
        budget = entity.budget_nano_usd
        return budget is not None and spent.get((level, scope_id), 0) >= budget

    for t in sorted(teams.values(), key=lambda t: str(t.id)):
        snap.teams.append(pb.Team(id=str(t.id), name=t.name))

    for k in sorted(keys, key=lambda k: str(k.id)):
        team = teams[k.team_id]
        owner_level, owner_scope_id = k.owner_scope
        owner_gone = False
        if k.owner_kind is KeyOwnerKind.USER:
            owner = members.get((k.team_id, k.user_id))
            # keys/reconciler.py suspends keys whose external membership was revoked.
            owner_gone = k.user_id not in active_users or (
                require_local_membership and owner is None
            )
        else:
            owner = service_accounts[k.service_account_id]
        levels = [(ScopeType.TEAM, team.id, team)]
        if owner is not None:
            levels.append((owner_level, owner_scope_id, owner))
        models: ModelSet = None
        for _, _, entity in levels:
            models = narrow(models, entity.model_access, entity.allowed_models)
        policy = pb.EffectivePolicy(
            allow_all_models=models is None,
            allowed_models=sorted(models or ()),
        )
        for level, scope_id, entity in levels:
            if (rl := rate_limit_scope(level, scope_id, entity)) is not None:
                policy.rate_limit_scopes.append(rl)

        if k.lifecycle is KeyLifecycle.SUSPENDED or owner_gone:
            status = pb.KEY_STATUS_SUSPENDED
        elif any(over_budget(level, scope_id, entity) for level, scope_id, entity in levels):
            status = pb.KEY_STATUS_BUDGET_EXCEEDED
            policy.budget_window_end_unix_ms = _ms(window_end)
        else:
            status = pb.KEY_STATUS_ACTIVE

        snap.api_keys.append(
            pb.ApiKey(
                key_id=str(k.id),
                key_hash=k.key_hash,
                team_id=str(k.team_id),
                status=status,
                expires_at_unix_ms=expiry_ms(k.expires_at),
                policy=policy,
            )
        )

    snap.required_capabilities.extend(capabilities_used(snap))

    # Defensive: everything above should already guarantee this. A snapshot the gateway
    # would NACK is never published.
    try:
        validate_snapshot(snap, max_bytes=max_bytes, max_keys=max_keys)
    except SnapshotInvalid as exc:
        raise CompileError(str(exc)) from exc
    for w in warnings:
        log.warning("compile %s: %s", scope, w)
    return CompiledSnapshot(
        snapshot=snap,
        source_revision=revision,
        key_count=len(snap.api_keys),
        warnings=tuple(warnings),
    )
