"""compile_scope: source state -> one resolved Snapshot (not yet versioned)."""

import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
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
    Alias,
    ApiKey,
    CatalogName,
    GatewaySession,
    KeyLifecycle,
    KeyOwnerKind,
    Model,
    ModelAccess,
    PolicyGrant,
    Router,
    RouterFallback,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
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
# TODO(c2): remove with the enum; any other name is emitted as CUSTOM.
ROUTING_STRATEGY = {
    "weighted": pb.ROUTING_STRATEGY_WEIGHTED,
    "least_in_flight": pb.ROUTING_STRATEGY_LEAST_IN_FLIGHT,
    "lowest_latency": pb.ROUTING_STRATEGY_LOWEST_LATENCY,
    "vllm_load": pb.ROUTING_STRATEGY_VLLM_LOAD,
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


class _WeightProjection:
    """TODO(c2): remove. The current proto holds weight on `Model`, the catalog on router
    members. Each model keeps its most used positive weight among weighted routers (1 if
    none); a member with another weight points at a synthetic internal `Model` per
    (model, weight), a copy that only differs in name and weight. Until c2 such a member's
    usage and metrics carry the synthetic name. Whitespace keeps it out of the API's names.
    """

    def __init__(
        self, models: Sequence[Model], weighted_members: Iterable[tuple[uuid.UUID, int]]
    ) -> None:
        self._models = {m.id: m for m in models}
        seen: dict[uuid.UUID, Counter[int]] = defaultdict(Counter)
        for model_id, weight in weighted_members:
            seen[model_id][weight] += 1
        self._canonical = {
            m.id: min(
                (w for w in seen[m.id] if w > 0), key=lambda w: (-seen[m.id][w], w), default=1
            )
            for m in models
        }
        self._taken = {m.name for m in models}
        self._synthetic: dict[tuple[uuid.UUID, int], str] = {}

    def member(self, model_id: uuid.UUID, weight: int, weighted: bool) -> str:
        """The wire model name of a member. Only `weighted` pools read the weight."""
        model = self._models[model_id]
        if not weighted or weight == self._canonical[model_id]:
            return model.name
        key = (model_id, weight)
        if key not in self._synthetic:
            name = f"{model.name} [weight {weight}]"
            while name in self._taken:
                name += "~"
            self._taken.add(name)
            self._synthetic[key] = name
        return self._synthetic[key]

    def wire_models(self, upstream_name: dict[uuid.UUID, str]) -> list[pb.Model]:
        def wire(model: Model, name: str, weight: int) -> pb.Model:
            out = pb.Model(
                name=name,
                upstream=upstream_name[model.upstream_id],
                upstream_model=model.upstream_model,
                weight=weight,
            )
            if model.price_input_nano_usd_per_mtok is not None:
                out.price.CopyFrom(
                    pb.Price(
                        input_nano_usd_per_mtok=model.price_input_nano_usd_per_mtok,
                        output_nano_usd_per_mtok=model.price_output_nano_usd_per_mtok or 0,
                    )
                )
            return out

        return [
            *(wire(m, m.name, self._canonical[i]) for i, m in self._models.items()),
            *(wire(self._models[i], n, w) for (i, w), n in self._synthetic.items()),
        ]


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
    - TODO(c2): remove the projection. The catalog is projected onto the current proto: a
      model is a `Model` plus a single-member `Router` of the same name, a router is a
      `Router` (member weights via `_WeightProjection`), an alias is a `Router` copying its
      target under the alias name, a fallback model is that model's own `Router`.
    - Source rows the gateway would reject are left out and reported in `warnings`:
      upstreams with an invalid base URL (and their models), models with an
      empty upstream_model, routers that end up without models (or with an empty traffic
      split), or are weighted with zero total weight (per split when split), aliases of
      those, and any name that is empty or reserved.
      Expiry: NULL -> 0 (never); at or before the epoch -> 1 ms (expired).
    - P2 routing: upstream health_check / circuit_breaker / metrics_url and router retry /
      fallbacks / traffic_split / sticky are emitted when set; `required_capabilities`
      lists exactly the capabilities those fields need (validate.capabilities_used, the
      gateway's rule). A retry policy with max_attempts NULL is emitted as 0, which the
      gateway reads as its default (2). Fallbacks to models that are left out are
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

    # Routing. Rename and rollback may have run in this session, hence populate_existing.
    upstreams = (await session.scalars(select(Upstream).order_by(Upstream.name))).all()
    upstream_name = {u.id: u.name for u in upstreams}
    catalog_rows = select(Model).order_by(Model.name).execution_options(populate_existing=True)
    models = (await session.scalars(catalog_rows)).all()
    catalog_rows = select(Router).order_by(Router.name).execution_options(populate_existing=True)
    routers = (await session.scalars(catalog_rows)).all()
    catalog_rows = select(Alias).order_by(Alias.name).execution_options(populate_existing=True)
    aliases = (await session.scalars(catalog_rows)).all()
    model_by_id = {d.id: d for d in models}
    advertised = {
        cap
        for caps in await session.scalars(
            select(GatewaySession.capabilities).where(
                GatewaySession.scope == scope, GatewaySession.connected.is_(True)
            )
        )
        for cap in caps
    }
    router_members: dict[uuid.UUID, list[RouterMember]] = {}
    for rm in await session.scalars(
        select(RouterMember).order_by(RouterMember.router_id, RouterMember.position)
    ):
        router_members.setdefault(rm.router_id, []).append(rm)
    fallbacks: dict[uuid.UUID, list[uuid.UUID]] = {}
    for rf in await session.scalars(
        select(RouterFallback).order_by(RouterFallback.router_id, RouterFallback.position)
    ):
        fallbacks.setdefault(rf.router_id, []).append(rf.model_id)
    split_rows = (
        await session.scalars(
            select(RouterSplit).order_by(RouterSplit.router_id, RouterSplit.position)
        )
    ).all()
    split_members: dict[uuid.UUID, list[RouterSplitMember]] = {}
    for sm in await session.scalars(
        select(RouterSplitMember).order_by(RouterSplitMember.split_id, RouterSplitMember.position)
    ):
        split_members.setdefault(sm.split_id, []).append(sm)
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

    def weighted_pools():
        for r in routers:
            if r.strategy == "weighted":
                yield router_members.get(r.id, [])
                for sr in splits.get(r.id, []):
                    yield split_members.get(sr.id, [])

    weights = _WeightProjection(
        valid_models,
        (
            (m.model_id, m.weight)
            for pool in weighted_pools()
            for m in pool
            if m.model_id in valid_model_ids
        ),
    )

    # By name id: what fallbacks and aliases can point at.
    projected: dict[uuid.UUID, pb.Router] = {}
    for d in valid_models:
        if d.name in RESERVED_ALIASES:
            warnings.append(f"model {d.name!r} not callable: the name is reserved")
            continue
        projected[d.id] = pb.Router(
            name=d.name, strategy=pb.ROUTING_STRATEGY_WEIGHTED, models=[d.name]
        )
    model_routers = dict(projected)

    for r in routers:
        members_of_router = router_members.get(r.id, [])
        dropped = [
            model_by_id[m.model_id].name
            for m in members_of_router
            if m.model_id not in valid_model_ids
        ]
        members_of_router = [m for m in members_of_router if m.model_id in valid_model_ids]
        if dropped:
            warnings.append(f"router {r.name!r}: left-out models {dropped} removed")
        if not r.name or r.name in RESERVED_ALIASES:
            warnings.append(f"router {r.name!r} left out: alias is empty or reserved")
            continue
        weighted = r.strategy == "weighted"
        router = pb.Router(
            name=r.name, strategy=ROUTING_STRATEGY.get(r.strategy, pb.ROUTING_STRATEGY_CUSTOM)
        )
        if router.strategy == pb.ROUTING_STRATEGY_CUSTOM:
            router.custom_strategy = r.strategy
            router.strategy_params_json = params_json(r.params)
            capability = f"strategy:{r.strategy}"
            if capability not in advertised:
                warnings.append(
                    f"router {r.name!r}: {capability} is not advertised by any connected gateway"
                )
        router_splits = splits.get(r.id, [])
        if router_splits:
            bad = None
            for sr in router_splits:
                pool = [m for m in split_members.get(sr.id, []) if m.model_id in valid_model_ids]
                if not pool:
                    bad = f"split {sr.name!r} has no models"
                elif weighted and not sum(m.weight for m in pool):
                    bad = f"split {sr.name!r} is weighted with zero total weight"
                if bad:
                    break
                router.traffic_split.append(
                    pb.TrafficSplit(
                        name=sr.name,
                        weight=sr.weight,
                        models=[weights.member(m.model_id, m.weight, weighted) for m in pool],
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
            if not members_of_router:
                warnings.append(f"router {r.name!r} left out: no models")
                continue
            if weighted and not sum(m.weight for m in members_of_router):
                warnings.append(f"router {r.name!r} left out: weighted with zero total weight")
                continue
            if r.sticky is not StickyBy.NONE:
                warnings.append(f"router {r.name!r}: sticky without a traffic split ignored")
        router.models.extend(
            weights.member(m.model_id, m.weight, weighted) for m in members_of_router
        )
        if r.retry_enabled:
            router.retry.CopyFrom(
                pb.RetryPolicy(
                    # 0 = the gateway default (2 attempts).
                    max_attempts=r.retry_max_attempts or 0,
                    retry_on_status=sorted(set(r.retry_on_status)),
                    total_timeout_ms=r.retry_total_timeout_ms,
                )
            )
        for fallback in fallbacks.get(r.id, []):
            if fallback in model_routers:
                router.fallbacks.append(model_routers[fallback].name)
            else:
                warnings.append(
                    f"router {r.name!r}: fallback {model_by_id[fallback].name!r} is left out,"
                    " dropped"
                )
        projected[r.id] = router

    wire_routers = list(projected.values())
    for a in aliases:
        target = projected.get(a.target_name_id)
        if not a.name or a.name in RESERVED_ALIASES:
            warnings.append(f"alias {a.name!r} left out: name is empty or reserved")
        elif target is None:
            warnings.append(f"alias {a.name!r} left out: {a.target_name!r} is left out")
        else:
            copy = pb.Router()
            copy.CopyFrom(target)
            copy.name = a.name
            wire_routers.append(copy)
    snap.routers.extend(sorted(wire_routers, key=lambda r: r.name))
    snap.models.extend(sorted(weights.wire_models(upstream_name), key=lambda m: m.name))

    # Teams, key owners and keys.
    teams = {t.id: t for t in await session.scalars(select(Team))}
    granted: dict[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None], list[str]] = {}
    for team_id, user_id, account_id, name in await session.execute(
        select(
            PolicyGrant.team_id,
            PolicyGrant.user_id,
            PolicyGrant.service_account_id,
            CatalogName.name,
        ).join(CatalogName, CatalogName.id == PolicyGrant.name_id)
    ):
        granted.setdefault((team_id, user_id, account_id), []).append(name)

    def allowed_names(entity: Team | TeamMember | ServiceAccount) -> list[str]:
        if isinstance(entity, Team):
            return granted.get((entity.id, None, None), [])
        if isinstance(entity, TeamMember):
            return granted.get((entity.team_id, entity.user_id, None), [])
        return granted.get((entity.team_id, None, entity.id), [])

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
        allowed: ModelSet = None
        for _, _, entity in levels:
            allowed = narrow(allowed, entity.model_access, allowed_names(entity))
        policy = pb.EffectivePolicy(
            allow_all_models=allowed is None,
            allowed_models=sorted(allowed or ()),
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
