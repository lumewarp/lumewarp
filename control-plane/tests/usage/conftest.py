"""The shared session database needs the default zone row for factory-built keys."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import Zone
from tests import factories


@pytest.fixture(autouse=True)
async def default_zone(session: AsyncSession) -> None:
    if await session.get(Zone, factories.DEFAULT_ZONE) is None:
        session.add(Zone(name=factories.DEFAULT_ZONE))
        await session.flush()
