"""compile_scope: source state -> one resolved Snapshot (not yet versioned)."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.strategy import BUILTIN_STRATEGIES, params_json
from lumewarp_control_plane.compiler.validate import (
    DEFAULT_BODY_RESERVED_KEYS,
    RESERVED_ALIASES,
    SnapshotInvalid,
    body_object_problem,
    capabilities_used,
    metrics_url_problem,
    reasoning_problem,
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
    MemberPolicy,
    Model,
    ModelAccess,
    Org,
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
from lumewarp_control_plane.quota import (
    org_effective,
    owner_effective,
    policy_of,
    team_effective,
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


# Effective name set: None = every name (universal), else exactly these names.
ModelSet = frozenset[str] | None


def narrow(parent: ModelSet, mode: ModelAccess, allowed: Sequence[str]) -> ModelSet:
    """One level of allowlist inheritance. INHERIT and ALL keep the parent's set (ALL on
    a team is the universal set); LIST intersects. An empty LIST is deny-all.
    """
    if mode is not ModelAccess.LIST:
        return parent
    own = frozenset(m for m in allowed if m)
    return own if parent is None else parent & own


def rate_limit_scope(prefix: str, scope_id: uuid.UUID | str, entity) -> pb.RateLimitScope | None:
    """One limiter scope for an entity whose *own* limits are set; NULL limits add none."""
    if entity.rpm_limit is None and entity.tpm_limit is None and entity.max_parallel is None:
        return None
    return pb.RateLimitScope(
        id=f"{prefix}:{scope_id}",
        limit=pb.RateLimit(
            requests_per_minute=entity.rpm_limit or 0,
            tokens_per_minute=entity.tpm_limit or 0,
            max_parallel_requests=entity.max_parallel or 0,
        ),
    )


def _owner_scope_limits(
    owner, member_defaults, team_eff, *, service_account: bool
) -> tuple[int | None, int | None, int | None]:
    """The owner level's limiter values, clamped by the team, one bucket per set value.

    A member exception wins over the team's `member_*_default`; a service account has no
    default. A dimension that is NULL at both the owner and the default stays NULL, so the
    owner level adds no extra bucket and the team's shared scope keeps enforcing it (a
    per-owner bucket for an inherited value would multiply the team's limit per owner).
    """
    if owner is not None:
        raw = (owner.rpm_limit, owner.tpm_limit, owner.max_parallel)
    else:
        raw = (None, None, None)
    if not service_account:
        raw = tuple(
            value if value is not None else default
            for value, default in zip(raw, member_defaults, strict=True)
        )
    parents = (team_eff.rpm_limit, team_eff.tpm_limit, team_eff.max_parallel)
    return tuple(
        None if value is None else (value if parent is None else min(value, parent))
        for value, parent in zip(raw, parents, strict=True)
    )


def _params_json(params: dict) -> str:
    """Wire form of a params object: empty means {}."""
    return params_json(params) if params else ""


def _wire_members(rows: Sequence[RouterMember | RouterSplitMember], names) -> list[pb.RouterMember]:
    return [
        pb.RouterMember(
            model=names[m.model_id], weight=m.weight, params_json=_params_json(m.params)
        )
        for m in rows
    ]


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_MS = timedelta(milliseconds=1)


_REASONING_MODES = {
    "passthrough": pb.REASONING_EFFORT_MODE_PASSTHROUGH,
    "strip": pb.REASONING_EFFORT_MODE_STRIP,
    "mapped": pb.REASONING_EFFORT_MODE_MAPPED,
}


def _model_body_rules(out: pb.Model, d: Model, warnings: list[str]) -> None:
    """Output bound, default body and reasoning. A stored setting the gateway would
    reject (the API refuses it) is left out with a warning instead of failing the compile.
    """
    if d.max_output_tokens:
        out.max_output_tokens = d.max_output_tokens
    if d.default_body:
        if problem := body_object_problem(
            d.default_body, "default_body", DEFAULT_BODY_RESERVED_KEYS
        ):
            warnings.append(f"model {d.name!r}: {problem}, left out")
        else:
            out.default_body_json = params_json(d.default_body)
    fragments = d.reasoning_fragments if d.reasoning_mode == "mapped" else None
    if d.reasoning_mode == "passthrough" and not d.reasoning_default_level:
        return
    if problem := reasoning_problem(d.reasoning_mode, fragments, d.reasoning_default_level):
        warnings.append(f"model {d.name!r}: reasoning {problem}, left out")
        return
    out.reasoning_effort.CopyFrom(
        pb.ReasoningEffort(
            mode=_REASONING_MODES[d.reasoning_mode],
            fragments_json={k: params_json(v) for k, v in (fragments or {}).items()},
            default_level=d.reasoning_default_level or "",
        )
    )


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
      Rate limits: one RateLimitScope per level that sets a limit (`org:<id>`,
      `team:<id>`, `member:<member_scope_id>`, `sa:<id>`, `key:<id>`; at most four per
      key). A member's level takes the member-policy exception, else the team's
      `member_*_default`, even without a membership row.
      Status: SUSPENDED from lifecycle, or for a user-owned key whose owner is no longer
      a member of the key's team (builtin only) or is deactivated; external owners without
      a local policy row inherit the team policy. Else BUDGET_EXCEEDED if the org (its
      spend derived from its teams), the team or the owner has a budget and the matching
      spend for the UTC month of `now` is >= it; else ACTIVE. Revoked keys are left out
      (the gateway answers 401 for them either way), so revocation is a key removal in
      deltas. Expiry is passed through as
      `expires_at_unix_ms` and enforced by the gateway.
    - The catalog is emitted as it is: `Model`, `Alias` (its target's current name) and
      `Router` (members with their weights and params, or splits with theirs). The gateway
      compiles the three kinds into one name lookup. `allowed_models` holds current names,
      so a rename puts the affected keys into the next delta.
    - Source rows the gateway would reject are left out and reported in `warnings`:
      upstreams with an invalid base URL (and their models), models with an
      empty upstream_model, routers that end up without models (or with an empty traffic
      split), or are weighted with zero total weight (per split when split), aliases of
      those, and any name that is empty or reserved.
      Expiry: NULL -> 0 (never); at or before the epoch -> 1 ms (expired).
    - Routing resilience: upstream health_check / circuit_breaker / metrics_url and router retry /
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
            api_key_ciphertext=u.api_key_ciphertext or b"",
            timeout_ms=u.timeout_ms,
            stream_usage_mode=STREAM_USAGE_MODE[u.stream_usage_mode],
        )
        if u.health_check_interval_ms is not None:
            up.health_check.CopyFrom(
                pb.HealthCheck(
                    url=u.health_check_url or "",
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
        elif d.name in RESERVED_ALIASES:
            warnings.append(f"model {d.name!r} left out: the name is reserved")
        else:
            valid_models.append(d)
            out = pb.Model(
                name=d.name, upstream=upstream_name[d.upstream_id], upstream_model=d.upstream_model
            )
            if d.price_input_nano_usd_per_mtok is not None:
                out.price.CopyFrom(
                    pb.Price(
                        input_nano_usd_per_mtok=d.price_input_nano_usd_per_mtok,
                        output_nano_usd_per_mtok=d.price_output_nano_usd_per_mtok or 0,
                    )
                )
            _model_body_rules(out, d, warnings)
            snap.models.append(out)
    valid_model_ids = {d.id for d in valid_models}
    model_name = {d.id: d.name for d in valid_models}

    # By name id: what aliases can point at.
    callable_names = dict(model_name)
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
            warnings.append(f"router {r.name!r} left out: name is empty or reserved")
            continue
        weighted = r.strategy == "weighted"
        builtin = r.strategy in BUILTIN_STRATEGIES
        router = pb.Router(
            name=r.name,
            strategy=r.strategy,
            # A strategy plugin always gets an object; a built-in takes no params.
            params_json=_params_json(r.params) if builtin else params_json(r.params),
        )
        capability = f"strategy:{r.strategy}"
        if not builtin and capability not in advertised:
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
                        name=sr.name, weight=sr.weight, members=_wire_members(pool, model_name)
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
            router.members.extend(_wire_members(members_of_router, model_name))
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
            if fallback in model_name:
                router.fallbacks.append(model_name[fallback])
            else:
                warnings.append(
                    f"router {r.name!r}: fallback {model_by_id[fallback].name!r} is left out,"
                    " dropped"
                )
        callable_names[r.id] = r.name
        snap.routers.append(router)

    for a in aliases:
        target = callable_names.get(a.target_name_id)
        if not a.name or a.name in RESERVED_ALIASES:
            warnings.append(f"alias {a.name!r} left out: name is empty or reserved")
        elif target is None:
            warnings.append(f"alias {a.name!r} left out: {a.target_name!r} is left out")
        else:
            snap.aliases.append(pb.Alias(name=a.name, target=target))

    # Python's order, not the database collation: same source state, same bytes.
    snap.models.sort(key=lambda m: m.name)
    snap.aliases.sort(key=lambda a: a.name)
    snap.routers.sort(key=lambda r: r.name)

    # Orgs, teams, key owners and keys: four levels of quotas. Archived teams keep their
    # rows for history but contribute nothing to the snapshot; they still count toward the
    # org's derived spend.
    orgs = {o.id: o for o in await session.scalars(select(Org))}
    all_teams = {t.id: t for t in await session.scalars(select(Team))}
    teams = {team_id: t for team_id, t in all_teams.items() if t.deleted_at is None}
    granted: dict[
        tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None],
        list[str],
    ] = {}
    for org_id, team_id, user_id, account_id, name in await session.execute(
        select(
            PolicyGrant.org_id,
            PolicyGrant.team_id,
            PolicyGrant.user_id,
            PolicyGrant.service_account_id,
            CatalogName.name,
        ).join(CatalogName, CatalogName.id == PolicyGrant.name_id)
    ):
        # The org id is dropped for every non-org grant: org grants are keyed by /org only.
        granted.setdefault((org_id, team_id, user_id, account_id), []).append(name)

    def allowed_names(entity: Org | Team | MemberPolicy | ServiceAccount) -> list[str]:
        if isinstance(entity, Org):
            return granted.get((entity.id, None, None, None), [])
        if isinstance(entity, Team):
            return granted.get((None, entity.id, None, None), [])
        if isinstance(entity, MemberPolicy):
            return granted.get((None, entity.team_id, entity.user_id, None), [])
        return granted.get((None, entity.team_id, None, entity.id), [])

    members = {(m.team_id, m.user_id): m for m in await session.scalars(select(TeamMember))}
    member_policies = {
        (m.team_id, m.user_id): m for m in await session.scalars(select(MemberPolicy))
    }
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
    # Org spend is derived: the sum of every team of the org (archived included), so the
    # org level needs no total row and can never drift from its teams.
    org_spent = {org_id: 0 for org_id in orgs}
    for team in all_teams.values():
        if team.org_id in org_spent:
            org_spent[team.org_id] += spent.get((ScopeType.TEAM, team.id), 0)

    def over_budget(level: ScopeType, scope_id: uuid.UUID, entity) -> bool:
        budget = entity.budget_nano_usd
        if budget is None:
            return False
        used = (
            org_spent.get(scope_id, 0)
            if level is ScopeType.ORG
            else spent.get((level, scope_id), 0)
        )
        return used >= budget

    for t in sorted(teams.values(), key=lambda t: str(t.id)):
        snap.teams.append(pb.Team(id=str(t.id), name=t.name))

    team_effective_policies = {
        team_id: team_effective(org_effective(orgs[team.org_id]), team)
        for team_id, team in teams.items()
        if team.org_id in orgs
    }

    for k in sorted(keys, key=lambda k: str(k.id)):
        team = teams.get(k.team_id)
        if team is None:
            # An archived team's keys are revoked by the archive transaction; a row left
            # behind must never reach the gateway.
            continue
        org = orgs[team.org_id]
        team_eff = team_effective_policies[team.id]
        member_defaults = (
            team.member_rpm_default,
            team.member_tpm_default,
            team.member_max_parallel_default,
        )
        owner_gone = False
        if k.owner_kind is KeyOwnerKind.USER:
            member = members.get((k.team_id, k.user_id))
            owner_policy = member_policies.get((k.team_id, k.user_id))
            # keys/reconciler.py suspends keys whose external membership was revoked.
            owner_gone = k.user_id not in active_users or (
                require_local_membership and member is None
            )
            service_account = False
        else:
            owner_policy = service_accounts[k.service_account_id]
            if owner_policy.deactivated_at is not None:
                # A deactivated service account's keys leave the snapshot; reactivating
                # the account brings the non-revoked ones back on the next compile.
                continue
            service_account = True
        owner_eff = owner_effective(
            team_eff,
            team,
            policy_of(owner_policy) if owner_policy is not None else None,
            service_account=service_account,
        )
        key_policy = pb.EffectivePolicy(
            allow_all_models=owner_eff.models is None,
            allowed_models=sorted(owner_eff.models or ()),
        )
        scopes: list[pb.RateLimitScope] = []
        if (rl := rate_limit_scope("org", org.id, org)) is not None:
            scopes.append(rl)
        if (rl := rate_limit_scope("team", team.id, team)) is not None:
            scopes.append(rl)
        if k.owner_kind is KeyOwnerKind.USER:
            # The member level's values are the exception, else the team's defaults,
            # even without a membership row (external authz backend). Emit the clamped
            # effective value so the scope matches /users/me.
            limits = _owner_scope_limits(
                owner_policy, member_defaults, team_eff, service_account=False
            )
            if any(value is not None for value in limits):
                scopes.append(
                    pb.RateLimitScope(
                        id=f"member:{k.owner_scope[1]}",
                        limit=pb.RateLimit(
                            requests_per_minute=limits[0] or 0,
                            tokens_per_minute=limits[1] or 0,
                            max_parallel_requests=limits[2] or 0,
                        ),
                    )
                )
        else:
            limits = _owner_scope_limits(
                owner_policy, member_defaults, team_eff, service_account=True
            )
            # A service account with no limit of its own adds no scope: the team's
            # scope already enforces the same effective value.
            if any(value is not None for value in limits):
                scopes.append(
                    pb.RateLimitScope(
                        id=f"sa:{k.owner_scope[1]}",
                        limit=pb.RateLimit(
                            requests_per_minute=limits[0] or 0,
                            tokens_per_minute=limits[1] or 0,
                            max_parallel_requests=limits[2] or 0,
                        ),
                    )
                )
        if (rl := rate_limit_scope("key", str(k.id), k)) is not None:
            scopes.append(rl)
        key_policy.rate_limit_scopes.extend(scopes)

        if k.lifecycle is KeyLifecycle.SUSPENDED or owner_gone:
            status = pb.KEY_STATUS_SUSPENDED
        elif (
            over_budget(ScopeType.ORG, org.id, org)
            or over_budget(ScopeType.TEAM, team.id, team)
            or (owner_policy is not None and over_budget(*k.owner_scope, owner_policy))
        ):
            status = pb.KEY_STATUS_BUDGET_EXCEEDED
            key_policy.budget_window_end_unix_ms = _ms(window_end)
        else:
            status = pb.KEY_STATUS_ACTIVE

        api_key = pb.ApiKey(
            key_id=str(k.id),
            key_hash=k.key_hash,
            team_id=str(k.team_id),
            status=status,
            expires_at_unix_ms=expiry_ms(k.expires_at),
            policy=key_policy,
        )
        # The previous hash carries its own expiry so a gateway booting from its persisted
        # snapshot keeps enforcing it while the control plane is down. Once it is past,
        # this compile omits it and the delta removes it from every gateway.
        if k.previous_key_hash and k.previous_key_expires_at is not None:
            previous_expires = expiry_ms(k.previous_key_expires_at)
            if k.expires_at is not None:
                previous_expires = min(previous_expires, expiry_ms(k.expires_at))
            if previous_expires > _ms(now):
                api_key.previous_key_hash = k.previous_key_hash
                api_key.previous_key_expires_at_unix_ms = previous_expires
        snap.api_keys.append(api_key)

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
