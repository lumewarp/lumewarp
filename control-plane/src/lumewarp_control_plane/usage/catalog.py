"""Bulk, kind- and event-time-aware provenance resolution.

Current names are valid only since their last rename (or creation). A historical
name owns the half-open interval from the preceding rename/creation to renamed_at.
Resolution uses request start time, with a configurable five-minute grace for a
unique nearby owner if no interval contains it. Same-kind reuse within that
window with a lagging gateway can still misattribute within an exact interval;
this is not a mapping
from config_version to the serving catalog.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import String, Uuid, any_, bindparam, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import (
    Alias,
    CatalogKind,
    CatalogName,
    CatalogNameHistory,
    Model,
    Router,
)


class CatalogResolutionSettings(BaseSettings):
    """Usage-owned settings; keep unrelated process settings outside this module."""

    model_config = SettingsConfigDict(env_prefix="LUMEWARP_USAGE_", extra="ignore")
    catalog_grace_seconds: float = Field(default=300, ge=0, allow_inf_nan=False)


async def resolve_catalog_ids(session: AsyncSession, events: Sequence[dict[str, Any]]) -> None:
    names = {event[kind.value] for event in events for kind in CatalogKind if event[kind.value]}
    for event in events:
        for kind in CatalogKind:
            event[f"{kind.value}_id"] = None
    if not names:
        return
    # Bind arrays rather than expanding IN lists: a 10k batch may have 30k
    # distinct names, and repeated lists would exceed asyncpg's bind limit.
    name_array = bindparam("usage_names", list(names), type_=ARRAY(String))
    history_ids = select(CatalogNameHistory.name_id).where(
        CatalogNameHistory.old_name == any_(name_array)
    )
    # Include current owners AND items that used to own one of these names. All
    # three typed tables are joined once, never one lookup per usage event.
    rows = (
        await session.execute(
            select(
                CatalogName, func.coalesce(Model.created_at, Alias.created_at, Router.created_at)
            )
            .outerjoin(Model, Model.name_id == CatalogName.id)
            .outerjoin(Alias, Alias.name_id == CatalogName.id)
            .outerjoin(Router, Router.name_id == CatalogName.id)
            .where(or_(CatalogName.name == any_(name_array), CatalogName.id.in_(history_ids)))
        )
    ).all()
    live = {row.id: (row, created) for row, created in rows}
    histories = (
        await session.scalars(
            select(CatalogNameHistory)
            .where(
                or_(
                    CatalogNameHistory.name_id
                    == any_(bindparam("usage_ids", list(live), type_=ARRAY(Uuid))),
                    CatalogNameHistory.old_name == any_(name_array),
                )
            )
            .order_by(CatalogNameHistory.renamed_at)
        )
    ).all()
    intervals: dict[tuple[CatalogKind, str], list[tuple[datetime | None, datetime | None, Any]]] = (
        defaultdict(list)
    )
    previous: dict[Any, datetime] = {}
    for history in histories:
        item = live.get(history.name_id)
        start = previous.get(history.name_id, item[1] if item else None)
        intervals[(history.kind, history.old_name)].append(
            (start, history.renamed_at, history.name_id)
        )
        previous[history.name_id] = history.renamed_at
    for row, created in rows:
        start = previous.get(row.id, created)
        if start is not None:
            intervals[(row.kind, row.name)].append((start, None, row.id))
    grace = timedelta(seconds=CatalogResolutionSettings().catalog_grace_seconds)
    for event in events:
        # Rejections have no serving model, even if an old gateway supplied a name.
        if not event["model"]:
            continue
        # The gateway binds the name at start, not at stream completion. Budget
        # accounting still uses completion time. Legacy gateways may omit start.
        ts = event.get("started_at") or event["ts"]
        for kind in CatalogKind:
            owners = intervals.get((kind, event[kind.value]), ())
            exact = {
                name_id
                for start, end, name_id in owners
                if (start is None or start <= ts) and (end is None or ts < end)
            }
            candidates = exact
            if not exact and grace:
                # Lag/skew tolerance, not snapshot reconstruction. Count ALL owners
                # within the window (including deleted ones), not just the nearest:
                # ambiguity must stay NULL. Reuse while an old gateway lags can
                # still misattribute, including within this grace window, in the new owner's
                # exact interval; config_version is deliberately not mapped here.
                candidates = {
                    name_id
                    for start, end, name_id in owners
                    if (start is not None and ts < start and start - ts <= grace)
                    or (end is not None and ts >= end and ts - end <= grace)
                }
            if len(candidates) == 1:
                name_id = next(iter(candidates))
                item = live.get(name_id)
                if item and item[0].kind == kind:
                    event[f"{kind.value}_id"] = name_id
