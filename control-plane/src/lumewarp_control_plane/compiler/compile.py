"""compile_scope: source state -> one resolved Snapshot (not yet versioned)."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import plugins as plugin_api
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.validate import (
    RESERVED_ALIASES,
    SnapshotInvalid,
    upstream_url_problem,
    validate_snapshot,
)
from lumewarp_control_plane.models import (
    ApiKey,
    Deployment,
    KeyLifecycle,
    ModelAccess,
    Org,
    Project,
    Route,
    RouteDeployment,
    RoutingStrategy,
    Scope,
    ScopeType,
    StreamUsageMode,
    Team,
    Upstream,
    UpstreamKind,
    UsageTotal,
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
}
# Strategies the P1 gateway can serve. A route using another one is left out of the
# snapshot (with a warning) rather than published, because the gateway would NACK the
# whole snapshot UNSUPPORTED_FEATURE and block unrelated changes such as revocations.
SUPPORTED_STRATEGIES = {RoutingStrategy.WEIGHTED, RoutingStrategy.LEAST_IN_FLIGHT}


class CompileError(Exception):
    """Source state cannot be compiled into a valid snapshot (e.g. over the snapshot
    size/key limits). Nothing is published; the outbox row is marked failed with this
    message, which must not contain secrets.
    """


@dataclass(frozen=True)
class CompiledSnapshot:
    """Result of `compile_scope`, not yet published.

    `snapshot.version` and `created_at_unix_ms` are 0 here; publishing assigns them.
    `warnings` lists source entities left out of the snapshot (e.g. an empty route).
    """

    snapshot: pb.Snapshot
    source_revision: int
    key_count: int
    warnings: tuple[str, ...] = field(default=())


# Effective model set: None = every model (universal), else exactly these aliases.
ModelSet = frozenset[str] | None


def narrow(parent: ModelSet, mode: ModelAccess, allowed: Sequence[str]) -> ModelSet:
    """One level of allowlist inheritance. INHERIT and ALL keep the parent's set (ALL on
    an org is the universal set); LIST intersects. An empty LIST is deny-all.
    """
    if mode is not ModelAccess.LIST:
        return parent
    own = frozenset(m for m in allowed if m)
    return own if parent is None else parent & own


def rate_limit_scope(level: ScopeType, entity) -> pb.RateLimitScope | None:
    if entity.rpm_limit is None and entity.tpm_limit is None and entity.max_parallel is None:
        return None
    return pb.RateLimitScope(
        id=f"{level.value}:{entity.id}",
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
    router_plugins: Sequence[plugin_api.LoadedPlugin] | None = None,
    max_snapshot_bytes: int | None = None,
    max_snapshot_keys: int | None = None,
) -> CompiledSnapshot:
    """Compile the current source state of `scope` into a snapshot.

    Pure over source state read in `session` (plus `now` for the budget window and the
    router plugins); the caller runs it in one REPEATABLE READ transaction, or holds the
    scope row lock, so every table is read at one consistent point and `source_revision`
    is `scopes.source_revision` as seen there.

    Contract:
    - Expands org -> team -> project -> key inheritance into each key's EffectivePolicy.
      Model access: resolve INHERIT first (a level with INHERIT takes its parent's
      effective set unchanged; orgs are never INHERIT), then intersect: ALL is the
      universal set, LIST is exactly `allowed_models`. `allow_all_models` is true iff the
      resulting effective set is still universal, e.g. ALL -> INHERIT -> INHERIT -> INHERIT
      (the defaults) allows every model. A LIST with an empty list anywhere on the path is
      deny-all (`allow_all_models=false`, `allowed_models=[]`), never "all".
      Rate limits: one RateLimitScope per level that sets a limit (id "<level>:<uuid>").
      Status: SUSPENDED from lifecycle; else BUDGET_EXCEEDED if any of key/project/team/
      org has a budget and its `usage_totals` for the UTC month of `now` is >= it; else
      ACTIVE. Revoked keys are left out (the gateway answers 401 for them either way), so
      revocation is a key removal in deltas. Expiry is passed through as
      `expires_at_unix_ms` and enforced by the gateway.
    - Source rows the gateway would reject are left out and reported in `warnings`:
      upstreams with an invalid base URL (and their deployments), deployments with an
      empty upstream_model, and routes that end up without deployments, use an
      unsupported strategy or a reserved alias, or are weighted with zero total weight.
      Expiry: NULL -> 0 (never); at or before the epoch -> 1 ms (expired).
    - The result is checked against every gateway invariant (`validate.validate_snapshot`,
      mirroring snapshot.rs::validate and Router::compile); a violation is a CompileError.
    - Router plugins (default: discovered `lumewarp.router` plugins) may override
      deployment weights; a failing plugin is skipped.
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
    deployments = (await session.scalars(select(Deployment).order_by(Deployment.name))).all()
    deployment_by_id = {d.id: d for d in deployments}
    routes = (await session.scalars(select(Route).order_by(Route.model))).all()
    members: dict[uuid.UUID, list[uuid.UUID]] = {}
    for rd in await session.scalars(
        select(RouteDeployment).order_by(RouteDeployment.route_id, RouteDeployment.position)
    ):
        members.setdefault(rd.route_id, []).append(rd.deployment_id)

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
        snap.upstreams.append(
            pb.Upstream(
                name=u.name,
                kind=UPSTREAM_KIND[u.kind],
                base_url=u.base_url,
                api_key_ref=u.api_key_ref or "",
                timeout_ms=u.timeout_ms,
                stream_usage_mode=STREAM_USAGE_MODE[u.stream_usage_mode],
            )
        )
    valid_deployments = []
    for d in deployments:
        if d.upstream_id not in valid_upstreams:
            warnings.append(f"deployment {d.name!r} left out: its upstream is left out")
        elif not d.name or not d.upstream_model:
            warnings.append(f"deployment {d.name!r} left out: empty name or upstream_model")
        else:
            valid_deployments.append(d)
    valid_deployment_ids = {d.id for d in valid_deployments}

    route_views: list[plugin_api.RouteView] = []
    for r in routes:
        member_ids = members.get(r.id, [])
        dropped = [deployment_by_id[i].name for i in member_ids if i not in valid_deployment_ids]
        member_ids = [i for i in member_ids if i in valid_deployment_ids]
        if dropped:
            warnings.append(f"route {r.model!r}: left-out deployments {dropped} removed")
        if not r.model or r.model in RESERVED_ALIASES:
            warnings.append(f"route {r.model!r} left out: alias is empty or reserved")
            continue
        if not member_ids:
            warnings.append(f"route {r.model!r} left out: no deployments")
            continue
        if r.strategy not in SUPPORTED_STRATEGIES:
            warnings.append(f"route {r.model!r} left out: strategy {r.strategy.value} unsupported")
            continue
        if r.strategy is RoutingStrategy.WEIGHTED and not sum(
            deployment_by_id[i].weight for i in member_ids
        ):
            warnings.append(f"route {r.model!r} left out: weighted with zero total weight")
            continue
        snap.routes.append(
            pb.ModelRoute(
                model=r.model,
                deployments=[deployment_by_id[i].name for i in member_ids],
                strategy=ROUTING_STRATEGY[r.strategy],
            )
        )
        route_views.append(
            plugin_api.RouteView(
                model=r.model,
                strategy=r.strategy.value,
                deployments=tuple(
                    plugin_api.DeploymentView(
                        name=d.name,
                        upstream=upstream_name[d.upstream_id],
                        upstream_model=d.upstream_model,
                        weight=d.weight,
                        price_input_nano_usd_per_mtok=d.price_input_nano_usd_per_mtok,
                        price_output_nano_usd_per_mtok=d.price_output_nano_usd_per_mtok,
                    )
                    for d in (deployment_by_id[i] for i in member_ids)
                ),
            )
        )

    if router_plugins is None:
        router_plugins = plugin_api.load_router_plugins()
    overrides = (
        await plugin_api.apply_router_plugins(route_views, router_plugins)
        if router_plugins and route_views
        else {}
    )
    for d in valid_deployments:
        dep = pb.Deployment(
            name=d.name,
            upstream=upstream_name[d.upstream_id],
            upstream_model=d.upstream_model,
            weight=overrides.get(d.name, d.weight),
        )
        if d.price_input_nano_usd_per_mtok is not None:
            dep.price.CopyFrom(
                pb.Price(
                    input_nano_usd_per_mtok=d.price_input_nano_usd_per_mtok,
                    output_nano_usd_per_mtok=d.price_output_nano_usd_per_mtok or 0,
                )
            )
        snap.deployments.append(dep)

    # Hierarchy and keys.
    orgs = {o.id: o for o in await session.scalars(select(Org))}
    teams = {t.id: t for t in await session.scalars(select(Team))}
    projects = {p.id: p for p in await session.scalars(select(Project))}
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

    def over_budget(level: ScopeType, entity) -> bool:
        budget = entity.budget_nano_usd
        return budget is not None and spent.get((level, entity.id), 0) >= budget

    for t in sorted(teams.values(), key=lambda t: str(t.id)):
        snap.teams.append(
            pb.Team(id=str(t.id), name=t.name, org_id=str(t.org_id), org_name=orgs[t.org_id].name)
        )

    for k in sorted(keys, key=lambda k: str(k.id)):
        org, team, project = orgs[k.org_id], teams[k.team_id], projects[k.project_id]
        levels = ((ScopeType.ORG, org), (ScopeType.TEAM, team), (ScopeType.PROJECT, project))
        levels += ((ScopeType.KEY, k),)
        models: ModelSet = None
        for _, entity in levels:
            models = narrow(models, entity.model_access, entity.allowed_models)
        policy = pb.EffectivePolicy(
            allow_all_models=models is None,
            allowed_models=sorted(models or ()),
        )
        for level, entity in levels:
            if (rl := rate_limit_scope(level, entity)) is not None:
                policy.rate_limit_scopes.append(rl)

        if k.lifecycle is KeyLifecycle.SUSPENDED:
            status = pb.KEY_STATUS_SUSPENDED
        elif any(over_budget(level, entity) for level, entity in levels):
            status = pb.KEY_STATUS_BUDGET_EXCEEDED
            policy.budget_window_end_unix_ms = _ms(window_end)
        else:
            status = pb.KEY_STATUS_ACTIVE

        snap.api_keys.append(
            pb.ApiKey(
                key_id=str(k.id),
                key_hash=k.key_hash,
                org_id=str(k.org_id),
                team_id=str(k.team_id),
                project_id=str(k.project_id),
                status=status,
                expires_at_unix_ms=expiry_ms(k.expires_at),
                policy=policy,
            )
        )

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
