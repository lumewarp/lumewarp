"""Bulk, kind- and event-time-aware provenance resolution.

Current names are valid only since their last rename (or creation). A historical
name owns the half-open interval from the preceding rename/creation to renamed_at.
This also prevents a newly reused name from stealing a delayed event.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

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
    intervals: dict[tuple[CatalogKind, str], list[tuple[datetime | None, datetime, Any]]] = (
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
    current = {
        (row.kind, row.name): (row.id, previous.get(row.id, created)) for row, created in rows
    }
    for event in events:
        # Rejections have no serving model, even if an old gateway supplied a name.
        if not event["model"]:
            continue
        ts = event["ts"]
        for kind in CatalogKind:
            key = (kind, event[kind.value])
            candidate = current.get(key)
            if candidate and candidate[1] is not None and ts >= candidate[1]:
                event[f"{kind.value}_id"] = candidate[0]
                continue
            for start, end, name_id in intervals.get(key, ()):
                if (start is None or start <= ts) and ts < end:
                    item = live.get(name_id)
                    if item and item[0].kind == kind:
                        event[f"{kind.value}_id"] = name_id
                    break
