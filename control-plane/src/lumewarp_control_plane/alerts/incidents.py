"""Incident history: grouped alerts, related incidents and the usage traffic around one.

An incident groups the alerts of one outage (see `alerts/receiver.py`). The traffic is read
from `usage_events` only; prompts and responses are never stored. Every query is bounded to
the incident window (at most 24 hours) and to the incident's zone, and the failed request
rows reuse the `/requests` metadata shape.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.api.key_refs import key_refs
from lumewarp_control_plane.api.requests import RequestOut
from lumewarp_control_plane.models import (
    Alert,
    AlertTarget,
    Incident,
    PlaygroundRequest,
    RequestOutcome,
    Team,
    UsageEvent,
)

MAX_TRAFFIC_WINDOW = timedelta(hours=24)
FAILED_REQUESTS_LIMIT = 50
LEADERBOARD_LIMIT = 10
RELATED_LIMIT = 50
# A stream cut after a 200 header is a failure with a 2xx status.
_FAILED = or_(UsageEvent.status >= 400, UsageEvent.outcome == RequestOutcome.UPSTREAM_ERROR)
# What the failed request list, `failed_count` and every `failed` count use.
_FAILED_OR_CODED = or_(_FAILED, UsageEvent.error_code.is_not(None))
# Served through a fallback and neither failed nor carrying an error code.
_RESCUED = and_(UsageEvent.fallback.is_(True), ~_FAILED_OR_CODED)
_STATUS_CLASS = case(
    (UsageEvent.status < 200, "other"),
    (UsageEvent.status < 300, "2xx"),
    (UsageEvent.status < 400, "3xx"),
    (UsageEvent.status < 500, "4xx"),
    (UsageEvent.status < 600, "5xx"),
    else_="other",
)


@dataclass
class Members:
    """An incident's member alerts (oldest first) and their current targets."""

    alerts: list[Alert] = field(default_factory=list)
    targets: list[AlertTarget] = field(default_factory=list)


def _text(annotations: dict, name: str) -> str | None:
    value = annotations.get(name)
    return value if isinstance(value, str) else None


def instance_of(alert: Alert) -> str | None:
    """The reporting gateway: the `instance` label, else `gateway_id`."""
    for name in ("instance", "gateway_id"):
        value = alert.labels.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def unique_targets(targets: Sequence[AlertTarget]) -> list[AlertTarget]:
    """One per `(kind, target_id)`, sorted by kind and name."""
    seen: dict[tuple[str, uuid.UUID], AlertTarget] = {}
    for target in targets:
        seen.setdefault((target.kind, target.target_id), target)
    return sorted(seen.values(), key=lambda row: (row.kind, row.name, str(row.target_id)))


def incident_row(incident: Incident, members: Members, now: datetime) -> dict:
    """The list row. Texts (`summary`, `description`, `lumewarp_summary`, `generator_url`)
    come from the first member, the earliest to start."""
    end = incident.ends_at or now
    duration = max(0, int((end - incident.starts_at).total_seconds()))
    first = members.alerts[0] if members.alerts else None
    annotations = first.annotations if first is not None else {}
    return {
        "id": incident.id,
        "alertname": incident.alertname,
        "severity": incident.severity,
        "status": incident.status,
        "lumewarp_status": incident.lumewarp_status,
        "zone": incident.zone_id,
        "starts_at": incident.starts_at,
        "ends_at": incident.ends_at,
        "duration_seconds": duration,
        "targets": [
            {"kind": target.kind, "name": target.name} for target in unique_targets(members.targets)
        ],
        "summary": _text(annotations, "summary"),
        "description": _text(annotations, "description"),
        "lumewarp_summary": _text(annotations, "lumewarp_summary"),
        "generator_url": first.generator_url if first is not None else None,
        "alert_count": len(members.alerts),
        "gateways": sorted({value for row in members.alerts if (value := instance_of(row))}),
    }


def member_row(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "starts_at": alert.starts_at,
        "ends_at": alert.ends_at,
        "status": alert.status,
        "instance": instance_of(alert),
    }


async def members_for(
    session: AsyncSession, incident_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Members]:
    out: dict[uuid.UUID, Members] = {incident_id: Members() for incident_id in incident_ids}
    if not incident_ids:
        return out
    alerts = list(
        await session.scalars(
            select(Alert)
            .where(Alert.incident_id.in_(incident_ids))
            .order_by(Alert.starts_at, Alert.id)
        )
    )
    by_alert = {row.id: row.incident_id for row in alerts}
    for row in alerts:
        out.setdefault(row.incident_id, Members()).alerts.append(row)
    if alerts:
        for target in await session.scalars(
            select(AlertTarget).where(AlertTarget.alert_id.in_(list(by_alert)))
        ):
            out[by_alert[target.alert_id]].targets.append(target)
    return out


async def related_incidents(
    session: AsyncSession, incident: Incident, targets: Sequence[AlertTarget], now: datetime
) -> list[dict]:
    """Other incidents sharing a target whose interval is within one hour of this one."""
    targets = unique_targets(targets)
    if not targets:
        return []
    shared = or_(
        *(
            and_(AlertTarget.kind == target.kind, AlertTarget.target_id == target.target_id)
            for target in targets
        )
    )
    start = incident.starts_at - timedelta(hours=1)
    end = (incident.ends_at or now) + timedelta(hours=1)
    related_ids = (
        select(Alert.incident_id)
        .join(AlertTarget, AlertTarget.alert_id == Alert.id)
        .where(shared, Alert.incident_id != incident.id)
    )
    rows = list(
        await session.scalars(
            select(Incident)
            .where(
                Incident.id.in_(related_ids),
                Incident.starts_at <= end,
                or_(Incident.ends_at.is_(None), Incident.ends_at >= start),
            )
            .order_by(Incident.starts_at.desc(), Incident.id.desc())
            .limit(RELATED_LIMIT)
        )
    )
    if not rows:
        return []
    members = await members_for(session, [row.id for row in rows])
    return [incident_row(row, members[row.id], now) for row in rows]


def _target_filters(targets: Sequence[AlertTarget]) -> list:
    filters: list = []
    for target in targets:
        if target.kind == "upstream":
            filters.append(UsageEvent.upstream == target.name)
            continue
        if target.kind not in {"model", "alias", "router"}:
            continue
        column = getattr(UsageEvent, target.kind)
        id_column = getattr(UsageEvent, f"{target.kind}_id")
        filters.append(
            or_(
                id_column == target.target_id,
                column == target.name,
                UsageEvent.requested == target.name,
            )
        )
    return filters


def _request_out(event: UsageEvent, playground_user_id: uuid.UUID | None, keys) -> RequestOut:
    return RequestOut(
        request_id=event.request_id,
        ts=event.ts,
        zone=event.zone,
        kind=event.kind,
        endpoint=event.endpoint,
        requested=event.requested,
        alias=event.alias,
        alias_id=event.alias_id,
        router=event.router,
        router_id=event.router_id,
        fallback=event.fallback,
        model=event.model,
        model_id=event.model_id,
        upstream=event.upstream,
        upstream_model=event.upstream_model,
        attempts=event.attempts,
        status=event.status,
        outcome=event.outcome,
        stream=event.stream,
        prompt_tokens=event.prompt_tokens,
        completion_tokens=event.completion_tokens,
        audio_ms=event.audio_ms,
        cost_nano_usd=event.cost_nano_usd,
        usage_state=event.usage_state,
        duration_ms=event.duration_ms,
        key_id=event.key_id,
        key=keys.get(event.key_id),
        team_id=event.team_id,
        owner_kind=event.owner_kind,
        owner_id=event.owner_id,
        playground_user_id=playground_user_id,
        error_code=event.error_code,
        ttft_ms=event.ttft_ms,
    )


def _rounded(value: object) -> float | None:
    """An aggregate as a float with one decimal; None when the minute had no sample."""
    return None if value is None else round(float(value), 1)


def traffic_window(incident: Incident, now: datetime) -> tuple[datetime, datetime, bool]:
    """The incident's traffic window, bounded to 24 hours and clipped to the newest data."""
    end = incident.ends_at or now
    start = incident.starts_at - timedelta(minutes=10)
    clipped = end - start > MAX_TRAFFIC_WINDOW
    if clipped:
        start = end - MAX_TRAFFIC_WINDOW
    if end < start:
        start = end
    return start, end, clipped


async def traffic_for(
    session: AsyncSession,
    incident: Incident,
    targets: Sequence[AlertTarget],
    now: datetime,
) -> dict:
    """Usage around an incident, bounded to 24 hours and the incident's targets."""
    start, end, clipped = traffic_window(incident, now)
    result: dict = {
        "window_start": start,
        "window_end": end,
        "clipped": clipped,
        "requests": 0,
        "failed_count": 0,
        "per_minute": [],
        "errors_by_code": [],
        "errors_by_status_class": [],
        "fallback_requests": 0,
        "fallback_share": 0.0,
        "rescued_count": 0,
        "attempts": [],
        "teams": [],
        "keys": [],
        "failed_requests": [],
    }
    target_filters = _target_filters(unique_targets(targets))
    if not target_filters:
        return result
    filters: list = [
        UsageEvent.ts >= start,
        UsageEvent.ts <= end,
        or_(*target_filters),
    ]
    if incident.zone_id is not None:
        filters.append(UsageEvent.zone == incident.zone_id)

    count = func.count()
    failed_count = func.count().filter(_FAILED_OR_CODED)
    # Served through a fallback and not failed, so requests = ok + fallback + failed.
    rescued_minute = func.count().filter(_RESCUED)
    total, failed_total = (await session.execute(select(count, failed_count).where(*filters))).one()
    result["requests"] = total or 0
    result["failed_count"] = failed_total or 0
    total = result["requests"]

    minute = func.date_trunc("minute", UsageEvent.ts)
    # TTFT exists only for streamed responses (NULL otherwise, which the aggregates skip);
    # context is the prompt tokens of requests that reported usage.
    ttft = UsageEvent.ttft_ms
    context = case((UsageEvent.prompt_tokens > 0, UsageEvent.prompt_tokens))
    result["per_minute"] = [
        {
            "minute": bucket,
            "requests": requests,
            "failed": failed,
            "fallback": fallback_ok,
            "ttft_avg_ms": _rounded(ttft_avg),
            "ttft_p95_ms": _rounded(ttft_p95),
            "context_avg_tokens": _rounded(context_avg),
            "context_p95_tokens": _rounded(context_p95),
        }
        for bucket, requests, failed, fallback_ok, ttft_avg, ttft_p95, context_avg, context_p95 in (
            await session.execute(
                select(
                    minute,
                    count,
                    failed_count,
                    rescued_minute,
                    func.avg(ttft),
                    func.percentile_cont(0.95).within_group(ttft),
                    func.avg(context),
                    func.percentile_cont(0.95).within_group(context),
                )
                .where(*filters)
                .group_by(minute)
                .order_by(minute)
            )
        )
    ]

    result["errors_by_code"] = [
        {"code": code, "count": errors}
        for code, errors in await session.execute(
            select(UsageEvent.error_code, count)
            .where(*filters, UsageEvent.error_code.is_not(None))
            .group_by(UsageEvent.error_code)
            .order_by(count.desc(), UsageEvent.error_code)
        )
    ]
    result["errors_by_status_class"] = [
        {"status_class": status_class, "count": errors}
        for status_class, errors in await session.execute(
            select(_STATUS_CLASS, count)
            .where(*filters, _FAILED)
            .group_by(_STATUS_CLASS)
            .order_by(count.desc(), _STATUS_CLASS)
        )
    ]

    fallback = func.count().filter(UsageEvent.fallback.is_(True))
    # The same predicate as the per-minute "via fallback" bucket.
    rescued = func.count().filter(_RESCUED)
    fallback_requests, rescued_count = (
        await session.execute(select(fallback, rescued).where(*filters))
    ).one()
    result["fallback_requests"] = fallback_requests or 0
    result["rescued_count"] = rescued_count or 0
    result["fallback_share"] = float(result["fallback_requests"]) / total if total else 0.0

    result["attempts"] = [
        {"attempts": attempts, "count": requests}
        for attempts, requests in await session.execute(
            select(UsageEvent.attempts, count)
            .where(*filters)
            .group_by(UsageEvent.attempts)
            .order_by(UsageEvent.attempts)
        )
    ]

    prompt = func.coalesce(func.sum(UsageEvent.prompt_tokens), 0)
    completion = func.coalesce(func.sum(UsageEvent.completion_tokens), 0)
    tokens = prompt + completion
    result["teams"] = [
        {
            "team_id": team_id,
            "team_name": team_name,
            "requests": requests,
            "failed": failed,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        for team_id, team_name, requests, failed, prompt_tokens, completion_tokens in (
            await session.execute(
                select(Team.id, Team.name, count, failed_count, prompt, completion)
                .join(Team, Team.id == UsageEvent.team_id)
                .where(*filters)
                .group_by(Team.id, Team.name)
                .order_by(count.desc(), tokens.desc(), Team.name)
                .limit(LEADERBOARD_LIMIT)
            )
        )
    ]

    key_rows = list(
        await session.execute(
            select(UsageEvent.key_id, count, failed_count, prompt, completion)
            .where(*filters)
            .group_by(UsageEvent.key_id)
            .order_by(count.desc(), tokens.desc(), UsageEvent.key_id)
            .limit(LEADERBOARD_LIMIT)
        )
    )
    refs = await key_refs(session, [row[0] for row in key_rows])
    result["keys"] = [
        {
            "key_id": key_id,
            "key": refs.get(key_id),
            "requests": requests,
            "failed": failed,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        for key_id, requests, failed, prompt_tokens, completion_tokens in key_rows
    ]

    failed = (
        select(UsageEvent, PlaygroundRequest.user_id)
        .outerjoin(PlaygroundRequest, PlaygroundRequest.request_id == UsageEvent.request_id)
        .where(*filters, _FAILED_OR_CODED)
        .order_by(UsageEvent.ts.desc(), UsageEvent.request_id.desc())
        .limit(FAILED_REQUESTS_LIMIT)
    )
    failed_rows = (await session.execute(failed)).all()
    failed_keys = await key_refs(session, [event.key_id for event, _ in failed_rows])
    result["failed_requests"] = [
        _request_out(event, playground_user_id, failed_keys)
        for event, playground_user_id in failed_rows
    ]
    return result
