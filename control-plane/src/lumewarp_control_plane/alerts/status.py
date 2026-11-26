"""The user-facing status view: which firing alerts touch names the caller can call.

Names are resolved exactly as `/users/me` resolves a member's effective models: per zone,
down the org -> team -> owner chain, with the team's member defaults. A name is callable in
a zone only when the zone's snapshot has a deployment for it; an upstream target expands to
the models deployed on that upstream in the zone.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.authz import Authz
from lumewarp_control_plane.models import (
    Alert,
    AlertTarget,
    Alias,
    CatalogName,
    MemberPolicy,
    ModelDeployment,
    Org,
    Router,
    RouterFallback,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
    Team,
    Zone,
    ZoneLimit,
    ZoneMemberDefaults,
)
from lumewarp_control_plane.quota import (
    org_effective,
    owner_effective,
    policy_of,
    team_effective,
)


@dataclass(frozen=True)
class StatusItem:
    name: str
    zone: str
    state: str
    summary: str | None
    since: datetime
    incident_id: uuid.UUID | None = None


async def _effective_models(
    session: AsyncSession, authz: Authz
) -> tuple[dict[str, set[str]], set[str]]:
    """The caller's allowed names per zone, plus the zones where the set is universal."""
    team_ids = list(await session.scalars(select(Team.id).where(Team.deleted_at.is_(None))))
    if not team_ids:
        return {}, set()
    readable = await authz.readable_teams(team_ids)
    if readable is not None:
        if not readable:
            return {}, set()
        team_ids = [team_id for team_id in team_ids if team_id in readable]
        if not team_ids:
            return {}, set()
    teams = list(await session.scalars(select(Team).where(Team.id.in_(team_ids))))
    zones = list(
        await session.scalars(select(Zone.id).where(Zone.archived_at.is_(None)).order_by(Zone.id))
    )
    if not zones:
        return {}, set()
    org_ids = {team.org_id for team in teams}
    orgs = {org.id: org for org in await session.scalars(select(Org).where(Org.id.in_(org_ids)))}
    owner_filters = [
        ZoneLimit.org_id.in_(org_ids),
        and_(
            ZoneLimit.team_id.in_(team_ids),
            ZoneLimit.user_id.is_(None),
            ZoneLimit.service_account_id.is_(None),
        ),
    ]
    if authz.user_id is not None:
        owner_filters.append(
            and_(ZoneLimit.team_id.in_(team_ids), ZoneLimit.user_id == authz.user_id)
        )
    limits = {
        (row.zone, row.org_id, row.team_id, row.user_id): row
        for row in await session.scalars(
            select(ZoneLimit).where(ZoneLimit.zone.in_(zones), or_(*owner_filters))
        )
    }
    defaults = {
        (row.zone, row.team_id): row
        for row in await session.scalars(
            select(ZoneMemberDefaults).where(
                ZoneMemberDefaults.zone.in_(zones),
                ZoneMemberDefaults.team_id.in_(team_ids),
            )
        )
    }
    member_policies: dict[uuid.UUID, MemberPolicy] = {}
    if authz.user_id is not None:
        member_policies = {
            row.team_id: row
            for row in await session.scalars(
                select(MemberPolicy).where(
                    MemberPolicy.team_id.in_(team_ids),
                    MemberPolicy.user_id == authz.user_id,
                )
            )
        }

    allowed: dict[str, set[str]] = {zone: set() for zone in zones}
    universal: set[str] = set()
    for team in teams:
        org = orgs[team.org_id]
        for zone in zones:
            org_limits = limits.get((zone, org.id, None, None))
            team_limits = limits.get((zone, None, team.id, None))
            org_policy = org_effective(org, org_limits)
            team_policy = team_effective(org_policy, team, team_limits)
            member_policy = member_policies.get(team.id)
            member_limits = limits.get((zone, None, team.id, authz.user_id))
            own = policy_of(member_policy, member_limits) if member_policy is not None else None
            effective = owner_effective(
                team_policy,
                defaults.get((zone, team.id)),
                own,
                service_account=False,
            )
            if effective.models is None:
                universal.add(zone)
            else:
                allowed[zone].update(effective.models)
    return allowed, universal


async def _callable_names(
    session: AsyncSession, zones: list[str]
) -> dict[str, dict[str, set[str]]]:
    """Per zone: each name the snapshot can call -> the model names it can be served by.

    Mirrors `compiler/compile.py`: a model exists only where it has a deployment; a router
    needs a deployed pool (every split of a split router must have one; a weighted strategy
    needs a non-zero weight; fallbacks do not make a router callable) and an alias follows
    its target. A router maps to its members, splits and deployed fallbacks, an alias to its
    target's models, so a model alert reaches the names that route to it.
    """
    names = {
        name_id: name
        for name_id, name in await session.execute(select(CatalogName.id, CatalogName.name))
    }
    deployed: dict[str, set[uuid.UUID]] = {zone: set() for zone in zones}
    for zone, name_id in await session.execute(
        select(ModelDeployment.zone, ModelDeployment.name_id)
    ):
        if zone in deployed:
            deployed[zone].add(name_id)
    strategies = {
        router_id: strategy
        for router_id, strategy in await session.execute(select(Router.name_id, Router.strategy))
    }
    members: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = {}
    for router_id, model_id, weight in await session.execute(
        select(RouterMember.router_id, RouterMember.model_id, RouterMember.weight)
    ):
        members.setdefault(router_id, []).append((model_id, weight))
    fallbacks: dict[uuid.UUID, set[uuid.UUID]] = {}
    for router_id, model_id in await session.execute(
        select(RouterFallback.router_id, RouterFallback.model_id)
    ):
        fallbacks.setdefault(router_id, set()).add(model_id)
    split_members: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = {}
    for split_id, model_id, weight in await session.execute(
        select(RouterSplitMember.split_id, RouterSplitMember.model_id, RouterSplitMember.weight)
    ):
        split_members.setdefault(split_id, []).append((model_id, weight))
    splits: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = {}
    for split_id, router_id, weight in await session.execute(
        select(RouterSplit.id, RouterSplit.router_id, RouterSplit.weight)
    ):
        splits.setdefault(router_id, []).append((split_id, weight))
    aliases = list(
        await session.execute(select(Alias.name_id, Alias.target_name_id, Alias.target_kind))
    )

    out: dict[str, dict[str, set[str]]] = {}
    for zone in zones:
        up = deployed[zone]
        served: dict[uuid.UUID, set[uuid.UUID]] = {model_id: {model_id} for model_id in up}
        for router_id, strategy in strategies.items():
            weighted = strategy == "weighted"
            pool: set[uuid.UUID] = set()
            if router_id in splits:
                bad = False
                for split_id, _weight in splits[router_id]:
                    rows = [m for m in split_members.get(split_id, []) if m[0] in up]
                    if not rows or (weighted and not sum(w for _, w in rows)):
                        bad = True
                        break
                    pool.update(model_id for model_id, _ in rows)
                if bad or not sum(w for _, w in splits[router_id]):
                    continue
            else:
                rows = [m for m in members.get(router_id, []) if m[0] in up]
                if not rows or (weighted and not sum(w for _, w in rows)):
                    continue
                pool.update(model_id for model_id, _ in rows)
            served[router_id] = pool | (fallbacks.get(router_id, set()) & up)
        for name_id, target_id, _kind in aliases:
            if target_id in served:
                served[name_id] = set(served[target_id])
        out[zone] = {
            names[name_id]: {names[m] for m in models if m in names}
            for name_id, models in served.items()
            if name_id in names
        }
    return out


_STATE_RANK = {"down": 0, "congested": 1, "degraded": 2}


@dataclass(frozen=True)
class Reach:
    """What the live zones can call, loaded once and shared by every alert or incident."""

    live_zones: list[str]
    callable_names: dict[str, dict[str, set[str]]]
    deployments_by_upstream: dict[tuple[str, uuid.UUID], set[str]]


async def load_reach(session: AsyncSession) -> Reach:
    live_zones = list(
        await session.scalars(select(Zone.id).where(Zone.archived_at.is_(None)).order_by(Zone.id))
    )
    callable_names = await _callable_names(session, live_zones) if live_zones else {}
    deployments_by_upstream: dict[tuple[str, uuid.UUID], set[str]] = {}
    if live_zones:
        for zone, upstream_id, name in await session.execute(
            select(ModelDeployment.zone, ModelDeployment.upstream_id, CatalogName.name).join(
                CatalogName, CatalogName.id == ModelDeployment.name_id
            )
        ):
            deployments_by_upstream.setdefault((zone, upstream_id), set()).add(name)
    return Reach(live_zones, callable_names, deployments_by_upstream)


def reached(
    reach: Reach, zone_id: str | None, targets: Iterable[AlertTarget]
) -> set[tuple[str, str]]:
    """Every `(zone, name)` callable in the zone(s) that the targets reach.

    A zone-less alert applies to every live zone. An upstream target reaches the models
    deployed on it in the zone; an alias or router target is itself a name callers use; a
    model target also reaches the names (aliases, routers) that can be served by it.
    """
    zones = (
        reach.live_zones if zone_id is None else [zone_id] if zone_id in reach.live_zones else []
    )
    targets = list(targets)
    out: set[tuple[str, str]] = set()
    for zone in zones:
        served_by = reach.callable_names.get(zone, {})
        models: set[str] = set()
        named: set[str] = set()
        for target in targets:
            if target.kind == "upstream":
                models |= reach.deployments_by_upstream.get((zone, target.target_id), set())
            else:
                named.add(target.name)
                models.add(target.name)
        out.update(
            (zone, name) for name, serving in served_by.items() if name in named or models & serving
        )
    return out


async def affected_names(
    session: AsyncSession, zone_id: str | None, targets: Sequence[AlertTarget]
) -> list[tuple[str, str]]:
    """The `(zone, name)` pairs any caller could see as affected, sorted; user-independent."""
    return sorted(reached(await load_reach(session), zone_id, targets))


async def status_items(session: AsyncSession, authz: Authz) -> list[StatusItem]:
    """Firing, user-visible alerts that intersect the caller's callable names."""
    reach = await load_reach(session)
    if not reach.live_zones:
        return []
    allowed, universal = await _effective_models(session, authz)
    is_admin = await authz.is_admin()
    names_by_zone: dict[str, set[str]] = {}
    for zone in reach.live_zones:
        if zone in universal:
            names_by_zone[zone] = set(reach.callable_names.get(zone, {}))
        else:
            names_by_zone[zone] = allowed.get(zone, set()) & set(reach.callable_names.get(zone, {}))
    if not any(names_by_zone.values()):
        return []

    alerts = list(
        await session.scalars(
            select(Alert)
            .where(Alert.status == "firing", Alert.lumewarp_status.is_not(None))
            .order_by(Alert.starts_at.desc(), Alert.id.desc())
        )
    )
    if not alerts:
        return []
    targets: dict[uuid.UUID, list[AlertTarget]] = {}
    for target in await session.scalars(
        select(AlertTarget).where(AlertTarget.alert_id.in_([alert.id for alert in alerts]))
    ):
        targets.setdefault(target.alert_id, []).append(target)

    # One item per (zone, name): the worst state wins, `since` is the earliest start among
    # the alerts of that state, and an admin's `incident_id` is that earliest alert's.
    chosen: dict[tuple[str, str], tuple[int, datetime, uuid.UUID, str | None, str]] = {}
    for alert in alerts:
        state = alert.lumewarp_status or ""
        rank = _STATE_RANK.get(state, len(_STATE_RANK))
        raw = alert.annotations.get("lumewarp_summary")
        summary = raw if isinstance(raw, str) else None
        for zone, name in reached(reach, alert.zone_id, targets.get(alert.id, [])):
            if name not in names_by_zone.get(zone, set()):
                continue
            key = (zone, name)
            current = chosen.get(key)
            if current is None or (rank, alert.starts_at) < (current[0], current[1]):
                chosen[key] = (rank, alert.starts_at, alert.incident_id, summary, state)
    items = [
        StatusItem(
            name=name,
            zone=zone,
            state=state,
            summary=summary,
            since=since,
            incident_id=incident_id if is_admin else None,
        )
        for (zone, name), (_rank, since, incident_id, summary, state) in chosen.items()
    ]
    return sorted(items, key=lambda item: (item.since, item.zone, item.name), reverse=True)
