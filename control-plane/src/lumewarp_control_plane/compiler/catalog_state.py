"""ID-preserving routing source state for rollback.

This deliberately excludes teams, keys, grants and all other policy state. The wire
snapshot remains the authority for published routing; this sidecar preserves database
identities and source-only fields that cannot be reconstructed from protobuf names.

TODO(c1): extend TABLES with catalog_names, aliases and member-local settings when the
catalog schema lands. This preparatory implementation uses the current routing schema.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Uuid, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import (
    Model,
    Router,
    RouterFallback,
    RouterModel,
    RouterSplit,
    RouterSplitModel,
    Upstream,
)

# Parent-before-child insertion order; deletion runs in reverse. No policy tables.
TABLES = (
    Upstream.__table__,
    Model.__table__,
    Router.__table__,
    RouterModel.__table__,
    RouterFallback.__table__,
    RouterSplit.__table__,
    RouterSplitModel.__table__,
)
ENTITY_TABLES = TABLES[:3]
EDGE_TABLES = TABLES[3:]
FORMAT_VERSION = 1


def _encode(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def capture_catalog_state(session: AsyncSession) -> dict[str, Any]:
    """Capture source rows in stable primary-key order, in the publisher's transaction."""
    tables = {}
    for table in TABLES:
        rows = await session.execute(select(table).order_by(*table.primary_key.columns))
        tables[table.name] = [
            {key: _encode(value) for key, value in row.items()} for row in rows.mappings()
        ]
    return {"format_version": FORMAT_VERSION, "tables": tables}


def _decode_state(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Validate the complete sidecar before modifying any source row."""
    if state.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported catalog_state format")
    source = state.get("tables")
    if not isinstance(source, dict) or set(source) != {table.name for table in TABLES}:
        raise ValueError("catalog_state has an unexpected table set")
    result = {}
    for table in TABLES:
        rows = source[table.name]
        if not isinstance(rows, list):
            raise ValueError(f"catalog_state table {table.name} must be a list")
        decoded = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(table.columns.keys()):
                raise ValueError(f"catalog_state table {table.name} has unexpected columns")
            values = dict(row)
            for column in table.columns:
                value = values[column.name]
                if value is not None and isinstance(column.type, Uuid):
                    values[column.name] = uuid.UUID(value)
                elif value is not None and isinstance(column.type, DateTime):
                    values[column.name] = datetime.fromisoformat(value)
            decoded.append(values)
        result[table.name] = decoded
    return result


async def restore_catalog_state(session: AsyncSession, state: dict[str, Any]) -> None:
    """Restore routing by ID, retaining identities across renames and recreating deletions.

    Existing entity rows are updated, not deleted/reinserted. Policy is never read or
    written here. The caller holds the scope/advisory locks and owns the transaction.
    """
    rows = _decode_state(state)
    await session.flush()
    # Release all RESTRICT graph edges before deleting absent entities.
    for table in reversed(EDGE_TABLES):
        await session.execute(delete(table))

    # A rename may have swapped/reused names. Vacate every current entity name before
    # assigning historical names; generated UUID names cannot collide with one another.
    for table in ENTITY_TABLES:
        ids = list(await session.scalars(select(table.c.id)))
        occupied = set(await session.scalars(select(table.c.name)))
        occupied.update(row["name"] for row in rows[table.name])
        for identity in ids:
            temporary = f"__rollback_{uuid.uuid4().hex}"
            while temporary in occupied:
                temporary = f"__rollback_{uuid.uuid4().hex}"
            occupied.add(temporary)
            await session.execute(
                update(table).where(table.c.id == identity).values(name=temporary)
            )

    # Remove stale models and routers first. A retained model may currently point at
    # an upstream absent from the old state; keep that upstream until its FK is moved.
    for table in reversed(ENTITY_TABLES[1:]):
        wanted = [row["id"] for row in rows[table.name]]
        await session.execute(delete(table).where(table.c.id.not_in(wanted)))

    for table in ENTITY_TABLES:
        existing = set(await session.scalars(select(table.c.id)))
        for row in rows[table.name]:
            if row["id"] in existing:
                await session.execute(update(table).where(table.c.id == row["id"]).values(**row))
            else:
                await session.execute(insert(table).values(**row))
    upstreams = Upstream.__table__
    await session.execute(
        delete(upstreams).where(upstreams.c.id.not_in([row["id"] for row in rows[upstreams.name]]))
    )
    for table in EDGE_TABLES:
        if rows[table.name]:
            await session.execute(insert(table), rows[table.name])
    # Core writes bypass identity-map synchronization. Callers may already have loaded
    # routing rows, so don't allow a later compiler read to reuse stale ORM attributes.
    routing_types = (
        Upstream,
        Model,
        Router,
        RouterModel,
        RouterFallback,
        RouterSplit,
        RouterSplitModel,
    )
    for instance in list(session.identity_map.values()):
        if isinstance(instance, routing_types):
            session.expire(instance)
