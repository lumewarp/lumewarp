"""ID-preserving catalog source state for rollback.

This deliberately excludes teams, keys, grants and all other policy state. The wire
snapshot remains the authority for published routing; this sidecar preserves database
identities and source-only fields that cannot be reconstructed from protobuf names.
"""

import base64
import binascii
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, LargeBinary, Uuid, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import catalog
from lumewarp_control_plane.models import (
    Alias,
    CatalogKind,
    CatalogName,
    Model,
    Router,
    RouterFallback,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
    Upstream,
)

_ENTITY_TYPES = (Upstream, CatalogName, Model, Router)
_EDGE_TYPES = (Alias, RouterMember, RouterFallback, RouterSplit, RouterSplitMember)
# Parent-before-child insertion order; deletion runs in reverse. No policy tables.
ENTITY_TABLES = tuple(cls.__table__ for cls in _ENTITY_TYPES)
EDGE_TABLES = tuple(cls.__table__ for cls in _EDGE_TYPES)
TABLES = (*ENTITY_TABLES, *EDGE_TABLES)
FORMAT_VERSION = 2


def _encode(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    # Only a sealed upstream API key (ciphertext) is binary.
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
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


def _decode_state(state: Any) -> dict[str, list[dict[str, Any]]]:
    """Validate the complete sidecar before modifying any source row.

    Errors never include data content from the sidecar.
    """
    if not isinstance(state, dict):
        raise ValueError("catalog_state must be an object")
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
                if value is None:
                    if not column.nullable:
                        raise ValueError(
                            f"catalog_state {table.name}.{column.name} must not be null"
                        )
                    continue
                try:
                    if isinstance(column.type, Uuid):
                        if not isinstance(value, str):
                            raise TypeError
                        values[column.name] = uuid.UUID(value)
                    elif isinstance(column.type, DateTime):
                        if not isinstance(value, str):
                            raise TypeError
                        values[column.name] = datetime.fromisoformat(value)
                    elif isinstance(column.type, LargeBinary):
                        if not isinstance(value, str):
                            raise TypeError
                        values[column.name] = base64.b64decode(value, validate=True)
                except (TypeError, ValueError, AttributeError, binascii.Error):
                    raise ValueError(
                        f"catalog_state {table.name}.{column.name} has an invalid value"
                    ) from None
            decoded.append(values)
        result[table.name] = decoded
    return result


def _pk(table) -> Any:
    return next(iter(table.primary_key.columns))


async def _vacate_names(session: AsyncSession, table, rows: list[dict[str, Any]]) -> None:
    """A rename may have swapped or reused names, so free every current one before
    assigning historical names. Generated names cannot collide with one another.
    """
    occupied = set(await session.scalars(select(table.c.name)))
    occupied.update(row["name"] for row in rows)
    for identity in list(await session.scalars(select(table.c.id))):
        temporary = f"__rollback_{uuid.uuid4().hex}"
        while temporary in occupied:
            temporary = f"__rollback_{uuid.uuid4().hex}"
        occupied.add(temporary)
        await session.execute(update(table).where(table.c.id == identity).values(name=temporary))


async def restore_catalog_state(session: AsyncSession, state: dict[str, Any]) -> None:
    """Restore the catalog by ID, keeping identities across renames and recreating deletions.

    Existing entity rows are updated, not deleted/reinserted, so grants on them survive.
    Names absent from the state are deleted and their grants (cascade) are audited; grants
    of recreated names do not come back. Names the restore changes get a history row. The
    caller holds the scope/advisory locks and owns the transaction.
    """
    rows = _decode_state(state)
    await session.flush()
    names = CatalogName.__table__
    upstreams = Upstream.__table__
    wanted_names = {row["id"]: row["name"] for row in rows[names.name]}
    current = (await session.execute(select(names.c.id, names.c.name, names.c.kind))).all()

    # Release all RESTRICT graph edges before deleting absent entities.
    for table in reversed(EDGE_TABLES):
        await session.execute(delete(table))

    stale = [(i, name) for i, name, _ in current if i not in wanted_names]
    await catalog.audit_grant_removal(session, stale)
    for table in (upstreams, names):
        await _vacate_names(session, table, rows[table.name])
    # Cascades to models, routers and grants. A retained model may still point at an
    # upstream absent from the state, so stale upstreams go once models have moved.
    await session.execute(delete(names).where(names.c.id.not_in(list(wanted_names))))

    for table in ENTITY_TABLES:
        key = _pk(table)
        existing = set(await session.scalars(select(key)))
        for row in rows[table.name]:
            if row[key.name] in existing:
                await session.execute(update(table).where(key == row[key.name]).values(**row))
            else:
                await session.execute(insert(table).values(**row))
    await session.execute(
        delete(upstreams).where(upstreams.c.id.not_in([row["id"] for row in rows[upstreams.name]]))
    )
    for table in EDGE_TABLES:
        if rows[table.name]:
            await session.execute(insert(table), rows[table.name])
    await catalog.record_renames(
        session,
        [
            (i, CatalogKind(kind), name)
            for i, name, kind in current
            if i in wanted_names and wanted_names[i] != name
        ],
    )
    # Core writes bypass identity-map synchronization. Callers may already have loaded
    # catalog rows, so don't allow a later compiler read to reuse stale ORM attributes.
    for instance in list(session.identity_map.values()):
        if isinstance(instance, (*_ENTITY_TYPES, *_EDGE_TYPES)):
            session.expire(instance)
