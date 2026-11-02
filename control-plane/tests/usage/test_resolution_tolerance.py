"""Start-time binding and bounded, ambiguity-rejecting lag/skew tolerance."""

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from lumewarp_control_plane.models import UsageEvent
from lumewarp_control_plane.usage import ingest
from lumewarp_control_plane.usage.catalog import CatalogResolutionSettings
from tests import factories
from tests.usage.test_catalog_provenance import CREATED, RENAMED, key_for, rename_at, wire


async def make_kind(session, kind, name, created, served):
    if kind == "model":
        return await factories.make_model(session, name, created_at=created)
    if kind == "router":
        return await factories.make_router(session, name, created_at=created)
    item = await factories.make_alias(session, name, served)
    item.created_at = created
    await session.flush()
    return item


@pytest.mark.parametrize("kind", ["model", "alias", "router"])
@pytest.mark.parametrize(
    "name, offset, start_offset, expected",
    [
        ("old", 60, -60, True),  # In-flight across rename.
        ("old", 25, None, True),  # Snapshot propagation lag / clock ahead.
        ("new", -25, None, True),  # Gateway clock behind.
        ("old", 0, None, True),  # Exact half-open end: zero-distance fallback.
        ("new", 0, None, True),  # Exact half-open start: exact owner.
        ("old", 300, None, True),  # Grace is inclusive.
        ("old", 301, None, False),
        ("new", -301, None, False),
    ],
)
async def test_start_time_lag_skew_and_exact_instant(
    session, kind, name, offset, start_offset, expected
):
    key = await key_for(session)
    served = await factories.make_model(session, "served", created_at=CREATED)
    item = await make_kind(session, kind, "old", CREATED, served)
    await rename_at(session, item, "new")
    fields = {kind: name}
    if start_offset is not None:
        fields["started_at_unix_ms"] = int(
            (RENAMED + timedelta(seconds=start_offset)).timestamp() * 1000
        )
    event = wire(key, timestamp=RENAMED + timedelta(seconds=offset), **fields)
    assert await ingest(session, "gateway", [event]) == 1
    row = await session.scalar(
        select(UsageEvent).where(UsageEvent.request_id == uuid.UUID(event.request_id))
    )
    assert getattr(row, f"{kind}_id") == (item.id if expected else None)


@pytest.mark.parametrize("kind", ["model", "alias", "router"])
@pytest.mark.parametrize(
    "reuse_after, event_after, started_after, owner",
    [
        (20, 10, None, None),  # Gap, both owners within grace: ambiguous.
        (1200, 10, None, "first"),  # Only former owner within grace.
        (1200, 1190, None, "second"),  # Only future owner within grace.
        (1200, 600, None, None),  # Neither nearby.
        (20, 60, -60, "first"),  # In-flight keeps original owner despite reuse.
        (20, 60, None, "second"),  # Known limit: exact new owner wins under lag.
        (1200, 1210, None, "second"),  # Reuse outside grace, exact new owner.
    ],
)
async def test_reuse_inside_and_outside_grace(
    session, kind, reuse_after, event_after, started_after, owner
):
    key = await key_for(session)
    served = await factories.make_model(session, "served", created_at=CREATED)
    first = await make_kind(session, kind, "old", CREATED, served)
    await rename_at(session, first, "new")
    second = await make_kind(session, kind, "old", RENAMED + timedelta(seconds=reuse_after), served)
    fields = {kind: "old"}
    if started_after is not None:
        fields["started_at_unix_ms"] = int(
            (RENAMED + timedelta(seconds=started_after)).timestamp() * 1000
        )
    event = wire(key, timestamp=RENAMED + timedelta(seconds=event_after), **fields)
    assert await ingest(session, "gateway", [event]) == 1
    row = await session.scalar(
        select(UsageEvent).where(UsageEvent.request_id == uuid.UUID(event.request_id))
    )
    assert getattr(row, f"{kind}_id") == {None: None, "first": first.id, "second": second.id}[owner]


async def test_creation_skew_and_configurable_grace(session, monkeypatch):
    key = await key_for(session)
    model = await factories.make_model(session, "served", created_at=CREATED)
    for grace, offset, expected in (
        (300, -25, True),
        (10, -25, False),
        (0, -1, False),
        (0, 0, True),
    ):
        monkeypatch.setenv("LUMEWARP_USAGE_CATALOG_GRACE_SECONDS", str(grace))
        event = wire(key, timestamp=CREATED + timedelta(seconds=offset))
        assert await ingest(session, "gateway", [event]) == 1
        row = await session.scalar(
            select(UsageEvent).where(UsageEvent.request_id == uuid.UUID(event.request_id))
        )
        assert row.model_id == (model.id if expected else None)
    monkeypatch.setenv("LUMEWARP_USAGE_CATALOG_GRACE_SECONDS", "-1")
    with pytest.raises(ValidationError):
        CatalogResolutionSettings()
