"""Seeding and a Python reference implementation of delta apply."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import outbox
from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2, snapshot_pb2
from lumewarp_control_plane.compiler.delta import ENTITIES
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import (
    ApiKey,
    ConfigVersion,
    Model,
    Router,
    Scope,
    ServiceAccount,
    Team,
    User,
)
from tests import factories

SCOPE = "default"
OCT = datetime(2026, 10, 15, tzinfo=UTC)


async def seed_scope(factory: SessionFactory, name: str = SCOPE) -> None:
    async with factory() as s, s.begin():
        s.add(Scope(name=name))


async def touch(factory: SessionFactory, reason: str = "test") -> int:
    """Commit an outbox row (a "mutation") and return its revision."""
    async with factory() as s, s.begin():
        return await outbox.enqueue_recompile(s, SCOPE, reason)


async def add_hierarchy(
    s: AsyncSession, *, team: dict | None = None, member: dict | None = None, name: str = "acme"
) -> tuple[Team, User]:
    """A team with one member-role user; returns both."""
    t = await factories.make_team(s, f"{name}-team", **(team or {}))
    user = await factories.make_user(s, f"{name}@example.com")
    await factories.make_member(s, t, user, **(member or {}))
    return t, user


async def add_key(
    s: AsyncSession, team: Team, owner: User | ServiceAccount, name: str = "k", **kw
) -> ApiKey:
    return await factories.make_key(s, team, owner, name=name, **kw)


async def add_router(
    s: AsyncSession,
    name: str,
    models: list[tuple[str, int | None]],
    *,
    upstream: str = "vllm",
    strategy: str = "weighted",
) -> Router:
    """models: (name, input price or None), created on `upstream` unless they exist."""
    up = await factories.make_upstream(s, upstream)
    members = []
    for model_name, price in models:
        model = await s.scalar(select(Model).where(Model.name == model_name))
        if model is None:
            model = await factories.make_model(
                s,
                model_name,
                up,
                price_input_nano_usd_per_mtok=price,
                price_output_nano_usd_per_mtok=price,
            )
        members.append(model)
    return await factories.make_router(s, name, members, strategy=strategy)


async def load_version(factory: SessionFactory, version: int) -> snapshot_pb2.Snapshot:
    async with factory() as s:
        row = await s.get(ConfigVersion, (SCOPE, version))
        return snapshot_pb2.Snapshot.FromString(row.payload)


def apply_delta(
    base: snapshot_pb2.Snapshot, delta: config_service_pb2.Delta
) -> snapshot_pb2.Snapshot:
    """Reference apply per docs/config-protocol.md: removes, then upserts by key; target
    metadata replaced. Entities are kept sorted by key, like the compiler emits them.
    """
    assert delta.base_version == base.version
    out = snapshot_pb2.Snapshot()
    out.CopyFrom(base)
    out.version = delta.version
    out.created_at_unix_ms = delta.created_at_unix_ms
    del out.required_capabilities[:]
    out.required_capabilities.extend(delta.required_capabilities)
    for field, upsert_field, remove_field, key in ENTITIES:
        items = {getattr(e, key): e for e in getattr(out, field)}
        for k in getattr(delta, remove_field):
            items.pop(k, None)
        for e in getattr(delta, upsert_field):
            items[getattr(e, key)] = e
        merged = [items[k] for k in sorted(items)]
        del getattr(out, field)[:]
        getattr(out, field).extend(merged)
    return out
