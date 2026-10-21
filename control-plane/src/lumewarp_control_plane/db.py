"""Async SQLAlchemy engine and session factory."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumewarp_control_plane.settings import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the engine. No connection is opened until first use."""
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request.

    The handler owns the transaction (`async with session.begin()` or an
    explicit commit); anything left uncommitted is rolled back on exit.
    """
    factory: SessionFactory = request.app.state.session_factory
    async with factory() as session:
        yield session
