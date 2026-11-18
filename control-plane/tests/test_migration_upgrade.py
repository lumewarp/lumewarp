"""The rebuilt baseline has no upgrade path: downgrades are refused.

Schema change while the baseline is unshipped happens by regenerating it; from the next
release on, every change is a new revision. A downgrade always asks for a database backup.
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane import migrate
from tests.conftest import create_database, drop_database


async def test_downgrade_is_refused() -> None:
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "head")
        with pytest.raises(NotImplementedError, match="restore"):
            await migrate.downgrade(engine, "base")
    finally:
        await engine.dispose()
        await drop_database(url)
