"""Derived catalog types, never persisted on aliases or routers."""

import uuid
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import (
    Alias,
    CatalogName,
    Model,
    ModelType,
    Router,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
)


async def called_name_type(session: AsyncSession, name: str) -> ModelType | None:
    """Resolve one callable name in one query; valid routers have homogeneous members."""
    target = (
        select(func.coalesce(Alias.target_name_id, CatalogName.id))
        .select_from(CatalogName)
        .outerjoin(Alias, Alias.name_id == CatalogName.id)
        .where(CatalogName.name == name)
        .scalar_subquery()
    )
    candidates = (
        select(Model.model_type)
        .where(Model.name_id == target)
        .union_all(
            select(Model.model_type)
            .join(RouterMember, RouterMember.model_id == Model.name_id)
            .where(RouterMember.router_id == target),
            select(Model.model_type)
            .join(RouterSplitMember, RouterSplitMember.model_id == Model.name_id)
            .join(RouterSplit, RouterSplit.id == RouterSplitMember.split_id)
            .where(RouterSplit.router_id == target),
        )
        .limit(1)
    )
    return await session.scalar(candidates)


async def catalog_types(session: AsyncSession) -> dict[uuid.UUID, ModelType | None]:
    """Load types in bounded queries, including split-only and empty routers."""
    types: dict[uuid.UUID, ModelType | None] = {
        id_: kind for id_, kind in (await session.execute(select(Model.name_id, Model.model_type)))
    }
    members: dict[uuid.UUID, set[ModelType | None]] = defaultdict(set)
    for router_id, model_id in await session.execute(
        select(RouterMember.router_id, RouterMember.model_id)
    ):
        members[router_id].add(types.get(model_id))
    for router_id, model_id in await session.execute(
        select(RouterSplit.router_id, RouterSplitMember.model_id).join(
            RouterSplitMember, RouterSplit.id == RouterSplitMember.split_id
        )
    ):
        members[router_id].add(types.get(model_id))
    for router_id in await session.scalars(select(Router.name_id)):
        kinds = members[router_id]
        types[router_id] = next(iter(kinds)) if len(kinds) == 1 else None
    for alias in await session.scalars(select(Alias)):
        types[alias.id] = types.get(alias.target_name_id)
    return types
