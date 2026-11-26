"""Upsert one Alertmanager webhook delivery and group its alerts into incidents.

The webhook payload is parsed in the API layer; this module owns the storage contract:
one row per `(fingerprint, startsAt)`, never deleted, with the labels and annotations
verbatim and the target list rewritten from the labels on every delivery.

Rules fire per gateway instance, so one outage arrives as several alerts. A new alert
joins the incident of its group (same alertname, same zone, same group key) whose interval
overlaps its own, or opens a new one; the group key is the sorted resolved targets, or the
labels minus the per-instance ones when nothing resolved. Membership is fixed at the first
receive. Candidate lookup is serialised per group with a transaction advisory lock, and
the incident row is locked while its window, status and worst severity/state are
recomputed from its members.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import Alert, AlertTarget, CatalogName, Incident, Upstream

# The largest values the columns accept; labels keep their full text in JSONB.
_MAX_LABEL = 64
# Labels that differ between the gateways (or pods) reporting the same outage.
INSTANCE_LABELS = frozenset({"instance", "gateway_id", "pod", "job", "endpoint", "service"})
# Worst first; an unknown value ranks after the known ones and NULL last.
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
STATE_RANK = {"down": 0, "congested": 1, "degraded": 2}


@dataclass(frozen=True)
class IncomingAlert:
    """One validated Alertmanager alert, with datetimes already parsed."""

    status: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime
    ends_at: datetime | None
    generator_url: str | None
    fingerprint: str


def _label(value: str | None, maximum: int = _MAX_LABEL) -> str | None:
    return value[:maximum] if value else None


def _resolve_ends_at(alert: IncomingAlert, now: datetime) -> datetime | None:
    """A firing alert has no end; a resolved one ends at its real `endsAt` (or now)."""
    if alert.status != "resolved":
        return None
    if alert.ends_at is None or alert.ends_at.year <= 1:
        return now
    return alert.ends_at


async def _catalog_targets(
    session: AsyncSession, names: Sequence[str]
) -> list[tuple[str, uuid.UUID, str]]:
    """Resolved catalog targets, keyed by the one installation-wide name namespace."""
    wanted = sorted({name for name in names if name})
    if not wanted:
        return []
    rows = (await session.scalars(select(CatalogName).where(CatalogName.name.in_(wanted)))).all()
    return [(row.kind.value, row.id, row.name) for row in rows]


async def _upstream_targets(
    session: AsyncSession, name: str, zone: str | None
) -> list[tuple[str, uuid.UUID, str]]:
    """Upstreams by name; a `zone` label limits the lookup to that zone."""
    if not name:
        return []
    stmt = select(Upstream).where(Upstream.name == name)
    if zone:
        stmt = stmt.where(Upstream.zone == zone)
    rows = (await session.scalars(stmt.order_by(Upstream.zone))).all()
    return [("upstream", row.id, row.name) for row in rows]


async def _targets(session: AsyncSession, alert: IncomingAlert) -> list[tuple[str, uuid.UUID, str]]:
    zone = _label(alert.labels.get("zone"))
    found = await _catalog_targets(
        session,
        [alert.labels.get("requested", ""), alert.labels.get("model", "")],
    )
    found += await _upstream_targets(session, alert.labels.get("upstream", ""), zone)
    # The same name may arrive from both `requested` and `model`; the target table's
    # primary key is (alert_id, kind, target_id), so drop exact duplicates.
    unique: list[tuple[str, uuid.UUID, str]] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    for kind, target_id, name in found:
        if (kind, target_id) in seen:
            continue
        seen.add((kind, target_id))
        unique.append((kind, target_id, name))
    return unique


def group_key(labels: dict[str, str], targets: Sequence[tuple[str, uuid.UUID, str]]) -> str:
    """The sha256 that groups alerts of one outage, beside alertname and zone."""
    if targets:
        raw = "targets:" + ",".join(sorted(f"{kind}:{target_id}" for kind, target_id, _ in targets))
    else:
        rest = {key: value for key, value in labels.items() if key not in INSTANCE_LABELS}
        raw = "labels:" + json.dumps(rest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def worst(values: Sequence[str | None], rank: dict[str, int]) -> str | None:
    present = [value for value in values if value]
    if not present:
        return None
    return min(present, key=lambda value: (rank.get(value, len(rank)), value))


async def _find_or_open_incident(
    session: AsyncSession,
    *,
    alertname: str,
    zone_id: str | None,
    key: str,
    starts_at: datetime,
    ends_at: datetime | None,
    status: str,
) -> tuple[uuid.UUID, bool]:
    """The overlapping incident of this group, locked, or a new one (`created` true)."""
    lock = f"lumewarp-incident:{alertname}:{zone_id or ''}:{key}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock, 0))))
    overlap = [
        Incident.alertname == alertname,
        Incident.zone_id.is_(None) if zone_id is None else Incident.zone_id == zone_id,
        Incident.group_key == key,
        or_(Incident.ends_at.is_(None), Incident.ends_at >= starts_at),
    ]
    if ends_at is not None:
        overlap.append(Incident.starts_at <= ends_at)
    found = await session.scalar(
        select(Incident.id)
        .where(*overlap)
        .order_by(Incident.starts_at.desc(), Incident.id.desc())
        .limit(1)
        .with_for_update()
    )
    if found is not None:
        return found, False
    incident = Incident(
        id=uuid7(),
        alertname=alertname,
        zone_id=zone_id,
        group_key=key,
        starts_at=starts_at,
        ends_at=ends_at,
        status=status,
    )
    session.add(incident)
    await session.flush()
    return incident.id, True


async def _recompute(session: AsyncSession, incident_id: uuid.UUID) -> None:
    """Rebuild the incident's window, status and worst severity/state from its members."""
    incident = await session.scalar(
        select(Incident).where(Incident.id == incident_id).with_for_update()
    )
    if incident is None:
        return
    members = (
        await session.execute(
            select(
                Alert.starts_at, Alert.ends_at, Alert.status, Alert.severity, Alert.lumewarp_status
            ).where(Alert.incident_id == incident_id)
        )
    ).all()
    if not members:
        return
    firing = any(row.status == "firing" for row in members)
    incident.starts_at = min(row.starts_at for row in members)
    incident.status = "firing" if firing else "resolved"
    incident.ends_at = (
        None if firing else max(row.ends_at for row in members if row.ends_at is not None)
    )
    incident.severity = worst([row.severity for row in members], SEVERITY_RANK)
    incident.lumewarp_status = worst([row.lumewarp_status for row in members], STATE_RANK)


async def receive(session: AsyncSession, alerts: Sequence[IncomingAlert]) -> int:
    """Upsert every alert in the caller's transaction and return how many were received.

    Replaying a delivery is idempotent: the row is updated in place and the target list is
    rewritten to exactly what the labels resolve to now. An unresolved name leaves the
    alert stored with no target row.
    """
    now = datetime.now(UTC)
    resolved = []
    for alert in alerts:
        targets = await _targets(session, alert)
        alertname = _label(alert.labels.get("alertname"), 256) or "unknown"
        zone_id = _label(alert.labels.get("zone"))
        resolved.append((alertname, zone_id, group_key(alert.labels, targets), alert, targets))
    # One global order for the advisory and row locks, so concurrent deliveries that touch
    # the same groups cannot deadlock each other.
    resolved.sort(key=lambda item: (item[0], item[1] or "", item[2]))
    for alertname, zone_id, key, alert, targets in resolved:
        starts_at = alert.starts_at.astimezone(UTC)
        values = {
            "id": uuid7(),
            "fingerprint": alert.fingerprint,
            "starts_at": starts_at,
            "ends_at": _resolve_ends_at(alert, now),
            "status": alert.status,
            "alertname": alertname,
            "severity": _label(alert.labels.get("severity")),
            "zone_id": zone_id,
            "lumewarp_status": _label(alert.labels.get("lumewarp_status")),
            "labels": alert.labels,
            "annotations": alert.annotations,
            "generator_url": alert.generator_url or None,
            "first_received_at": now,
            "last_received_at": now,
        }
        incident_id = await session.scalar(
            select(Alert.incident_id).where(
                Alert.fingerprint == alert.fingerprint, Alert.starts_at == starts_at
            )
        )
        opened: uuid.UUID | None = None
        if incident_id is None:
            incident_id, created = await _find_or_open_incident(
                session,
                alertname=alertname,
                zone_id=zone_id,
                key=key,
                starts_at=starts_at,
                ends_at=values["ends_at"],
                status=alert.status,
            )
            if created:
                opened = incident_id
        values["incident_id"] = incident_id
        stmt = (
            pg_insert(Alert)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[Alert.fingerprint, Alert.starts_at],
                set_={
                    # A stale firing redelivery must not reopen a resolved alert, nor
                    # move its end: a row stays resolved once it has resolved.
                    "ends_at": case(
                        (Alert.status == "resolved", Alert.ends_at),
                        else_=values["ends_at"],
                    ),
                    "status": case(
                        (Alert.status == "resolved", Alert.status),
                        else_=values["status"],
                    ),
                    "alertname": values["alertname"],
                    "severity": values["severity"],
                    "zone_id": values["zone_id"],
                    "lumewarp_status": values["lumewarp_status"],
                    "labels": values["labels"],
                    "annotations": values["annotations"],
                    "generator_url": values["generator_url"],
                    "last_received_at": now,
                },
            )
            .returning(Alert.id, Alert.incident_id)
        )
        alert_id, attached = (await session.execute(stmt)).one()
        if opened is not None and attached != opened:
            # A concurrent delivery of the same alert won the insert: drop the empty
            # incident this one opened and keep the row's membership. An existing
            # incident found here is never deleted.
            await session.execute(delete(Incident).where(Incident.id == opened))
        await session.execute(delete(AlertTarget).where(AlertTarget.alert_id == alert_id))
        session.add_all(
            AlertTarget(alert_id=alert_id, kind=kind, target_id=target_id, name=name)
            for kind, target_id, name in targets
        )
        await session.flush()
        await _recompute(session, attached)
    return len(alerts)
