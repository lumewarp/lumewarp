"""Incident history and incident detail traffic.

An incident groups the alerts of one outage (one alertname, zone and target set with
overlapping intervals), so a rule firing per gateway gives one incident "reported by N
gateways". Rows are never deleted. The traffic view is metadata from `usage_events` only;
Lumewarp shows its own data and runs no Prometheus queries (`generator_url` links out).
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import exists, select

from lumewarp_control_plane.alerts.incidents import (
    incident_row,
    member_row,
    members_for,
    related_incidents,
    traffic_for,
)
from lumewarp_control_plane.alerts.status import affected_names
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    aware,
    ensure_range,
    validation_error,
)
from lumewarp_control_plane.api.key_refs import KeyRefOut
from lumewarp_control_plane.api.requests import RequestOut
from lumewarp_control_plane.api.schemas import Page, ReadModel
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import Alert, AlertTarget, Incident

router = APIRouter(tags=["usage"])

IncidentState = Literal["firing", "resolved"]
Impact = Literal["user_visible", "admin_only"]

FromQuery = Annotated[
    datetime | None,
    Query(alias="from", description="Only incidents that started at or after this instant."),
]
ToQuery = Annotated[
    datetime | None,
    Query(alias="to", description="Only incidents that started before this instant."),
]


class IncidentTargetOut(ReadModel):
    kind: Literal["model", "alias", "router", "upstream"]
    name: str


class IncidentOut(ReadModel):
    id: uuid.UUID = Field(description="The incident id.")
    alertname: str
    severity: str | None = Field(
        description="The worst member's `severity` label (critical, warning, info)."
    )
    status: IncidentState = Field(
        description="`firing` while any member alert fires, else `resolved`."
    )
    lumewarp_status: str | None = Field(
        description=(
            "The worst member's `lumewarp_status` (down, congested, degraded); null = "
            "admin-only, never shown to users."
        )
    )
    zone: str | None = Field(description="The alerts' `zone` label; null = every zone.")
    starts_at: datetime = Field(description="The earliest member start.")
    ends_at: datetime | None = Field(
        description="The latest member end once all members resolved; null while firing."
    )
    duration_seconds: int
    targets: list[IncidentTargetOut] = Field(
        description="The resolved targets of the member alerts, deduplicated."
    )
    summary: str | None = Field(description="The first member's `summary` annotation.")
    description: str | None = Field(description="The first member's `description` annotation.")
    lumewarp_summary: str | None = Field(
        description="The first member's `lumewarp_summary` annotation: the user banner text."
    )
    generator_url: str | None = Field(
        description="The first member's link back to the Prometheus expression."
    )
    alert_count: int = Field(description="Number of member alerts.")
    gateways: list[str] = Field(
        description=(
            "Sorted distinct reporting gateways of the members (`instance` label, else "
            "`gateway_id`); empty when no member has either."
        )
    )


class IncidentAlertOut(ReadModel):
    id: uuid.UUID = Field(description="The member alert row.")
    starts_at: datetime
    ends_at: datetime | None
    status: IncidentState
    instance: str | None = Field(
        description="The alert's `instance` label, else `gateway_id`; null when neither."
    )


class IncidentAffectedOut(ReadModel):
    zone: str
    name: str = Field(
        description="A callable model, alias or router name the incident's targets reach."
    )


class IncidentMinuteOut(ReadModel):
    minute: datetime
    requests: int
    failed: int = Field(description="Requests of the minute that failed (as in `failed_requests`).")
    fallback: int = Field(
        description=(
            "Requests of the minute served through a fallback and not failed; the rest of "
            "`requests` minus `failed` succeeded directly."
        )
    )
    ttft_avg_ms: float | None = Field(
        description="Mean time to first token of the minute's streamed requests; null = none."
    )
    ttft_p95_ms: float | None = Field(
        description="95th percentile time to first token of the minute's streamed requests."
    )
    context_avg_tokens: float | None = Field(
        description="Mean prompt tokens of the minute's requests that reported usage."
    )
    context_p95_tokens: float | None = Field(
        description="95th percentile prompt tokens of the minute's requests that reported usage."
    )


class IncidentErrorCodeOut(ReadModel):
    code: str
    count: int


class IncidentStatusClassOut(ReadModel):
    status_class: str
    count: int


class IncidentAttemptsOut(ReadModel):
    attempts: int
    count: int


class IncidentTeamTrafficOut(ReadModel):
    team_id: uuid.UUID
    team_name: str
    requests: int
    failed: int = Field(description="The team's failed requests in the window.")
    prompt_tokens: int
    completion_tokens: int


class IncidentKeyTrafficOut(ReadModel):
    key_id: uuid.UUID
    key: KeyRefOut | None
    requests: int
    failed: int = Field(description="The key's failed requests in the window.")
    prompt_tokens: int
    completion_tokens: int


class IncidentTrafficOut(ReadModel):
    window_start: datetime
    window_end: datetime
    clipped: bool = Field(
        description="True when the incident window was cut to the newest 24 hours."
    )
    requests: int
    failed_count: int = Field(
        description=(
            "Requests in the window with a status of 400 or more, a cut stream or an error "
            "code: the predicate of `failed_requests`, uncapped."
        )
    )
    per_minute: list[IncidentMinuteOut]
    errors_by_code: list[IncidentErrorCodeOut]
    errors_by_status_class: list[IncidentStatusClassOut]
    fallback_requests: int
    fallback_share: float
    rescued_count: int
    attempts: list[IncidentAttemptsOut]
    teams: list[IncidentTeamTrafficOut]
    keys: list[IncidentKeyTrafficOut]
    failed_requests: list[RequestOut]


class IncidentDetailOut(IncidentOut):
    alerts: list[IncidentAlertOut] = Field(description="The member alerts, oldest first.")
    affected: list[IncidentAffectedOut] = Field(
        description=(
            "Every callable name the targets reach, by zone (a zone-less incident covers "
            "every live zone), independent of any caller's grants."
        )
    )
    related: list[IncidentOut] = Field(
        description="Other incidents sharing a target within one hour of this one."
    )
    traffic: IncidentTrafficOut


def _encode_cursor(starts_at: datetime, alert_id: uuid.UUID) -> str:
    raw = json.dumps([starts_at.isoformat(), str(alert_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError("cursor shape")
        ts_value, alert_value = values
        if not isinstance(ts_value, str) or not isinstance(alert_value, str):
            raise ValueError("cursor shape")
        starts_at = datetime.fromisoformat(ts_value)
        if starts_at.tzinfo is None:
            raise ValueError("cursor timestamp")
        alert_id = uuid.UUID(alert_value)
    except (TypeError, ValueError) as exc:
        raise validation_error("cursor", "invalid cursor") from exc
    return starts_at.astimezone(UTC), alert_id


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/incidents", response_model=Page[IncidentOut], operation_id="listIncidents")
async def list_incidents(
    authz: Authz,
    session: SessionDep,
    zone: Annotated[
        str | None,
        Query(description="Only this zone's incidents, plus alerts without a zone."),
    ] = None,
    status_filter: Annotated[
        IncidentState | None, Query(alias="status", description="Only firing or resolved.")
    ] = None,
    impact: Annotated[
        Impact | None,
        Query(
            description=(
                "`user_visible`: incidents with a `lumewarp_status`; `admin_only`: without one."
            )
        ),
    ] = None,
    q: Annotated[
        str | None,
        Query(
            max_length=256,
            description=(
                "Case-insensitive substring of the alertname, a member's `summary` "
                "annotation or a resolved target name."
            ),
        ),
    ] = None,
    from_: FromQuery = None,
    to_: ToQuery = None,
    limit: LimitDep = 50,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from the previous page's `next_cursor`."),
    ] = None,
) -> Page[IncidentOut]:
    """Incidents, newest `starts_at` first. Requires an installation admin.

    An incident groups the alerts of one outage: same alertname, zone and resolved targets
    (or labels minus the per-instance ones), with overlapping intervals. `zone` also
    includes incidents without a zone label (they apply everywhere); `q` matches the
    alertname, a member's `summary` or any resolved model, alias, router or upstream name.
    Rows are never deleted, so the history is complete.
    """
    await authz.require(Action.ROUTING_READ)
    start = aware(from_, "from")
    end = aware(to_, "to")
    if start is not None and end is not None:
        if end <= start:
            raise validation_error("to", "must be after from")
        ensure_range(start, end)
    filters = []
    if zone is not None:
        filters.append((Incident.zone_id == zone) | Incident.zone_id.is_(None))
    if status_filter is not None:
        filters.append(Incident.status == status_filter)
    if impact == "user_visible":
        filters.append(Incident.lumewarp_status.is_not(None))
    elif impact == "admin_only":
        filters.append(Incident.lumewarp_status.is_(None))
    if q is not None and q.strip():
        pattern = _like(q.strip())
        summary_hit = exists().where(
            Alert.incident_id == Incident.id,
            Alert.annotations["summary"].astext.ilike(pattern, escape="\\"),
        )
        target_hit = exists().where(
            Alert.incident_id == Incident.id,
            AlertTarget.alert_id == Alert.id,
            AlertTarget.name.ilike(pattern, escape="\\"),
        )
        filters.append(Incident.alertname.ilike(pattern, escape="\\") | summary_hit | target_hit)
    if start is not None:
        filters.append(Incident.starts_at >= start)
    if end is not None:
        filters.append(Incident.starts_at < end)
    stmt = select(Incident).where(*filters).order_by(Incident.starts_at.desc(), Incident.id.desc())
    if cursor is not None:
        cursor_starts, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            (Incident.starts_at < cursor_starts)
            | ((Incident.starts_at == cursor_starts) & (Incident.id < cursor_id))
        )
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    next_cursor = None
    if len(rows) > limit:
        next_cursor = _encode_cursor(rows[limit - 1].starts_at, rows[limit - 1].id)
    page_rows = rows[:limit]
    members = await members_for(session, [row.id for row in page_rows])
    now = datetime.now(UTC)
    return Page[IncidentOut](
        items=[IncidentOut(**incident_row(row, members[row.id], now)) for row in page_rows],
        next_cursor=next_cursor,
    )


@router.get(
    "/incidents/{incident_id}",
    response_model=IncidentDetailOut,
    operation_id="getIncident",
)
async def get_incident(
    incident_id: uuid.UUID, authz: Authz, session: SessionDep
) -> IncidentDetailOut:
    """One incident with its member alerts, affected names, related incidents and the usage
    traffic around it.

    Requires an installation admin. `traffic` reads metadata from the incident window
    (`starts_at - 10 min` to `ends_at` or now), clipped to the newest 24 hours and restricted
    to the incident's targets and zone. `affected` lists every callable name the targets
    reach, whoever may call it. `related` lists other incidents sharing a target within one
    hour of the incident. Prompts and responses are never included.
    """
    await authz.require(Action.ROUTING_READ)
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise not_found("incident not found")
    now = datetime.now(UTC)
    members = (await members_for(session, [incident.id]))[incident.id]
    related = await related_incidents(session, incident, members.targets, now)
    traffic = await traffic_for(session, incident, members.targets, now)
    affected = await affected_names(session, incident.zone_id, members.targets)
    return IncidentDetailOut(
        **incident_row(incident, members, now),
        alerts=[IncidentAlertOut(**member_row(row)) for row in members.alerts],
        affected=[IncidentAffectedOut(zone=zone, name=name) for zone, name in affected],
        related=[IncidentOut(**row) for row in related],
        traffic=IncidentTrafficOut(**traffic),
    )
