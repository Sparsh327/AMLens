"""Database access.

- Postgres: async SQLAlchemy engine + session factory (the app's own DB).
- MS SQL:   a tiny async ping via aioodbc. The real read-only warehouse access
            layer comes with the text-to-SQL tool (F4).
"""

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from amlens.core.config import Settings, get_settings


class Base(DeclarativeBase):
    """Base class for all ORM models (each module's models.py)."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(settings.postgres_dsn, pool_pre_ping=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, closed afterwards."""
    get_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def ping_postgres(settings: Settings) -> None:
    """Raise if Postgres does not answer `SELECT 1` within the timeout."""
    engine = get_engine(settings)

    async def _ping() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    await asyncio.wait_for(_ping(), timeout=settings.health_check_timeout_s)


async def ping_mssql(settings: Settings) -> None:
    """Raise if MS SQL does not answer `SELECT 1` within the timeout.

    aioodbc is imported lazily: it needs the system ODBC driver manager
    (unixODBC) at import time, and the app should still boot without it.
    """
    import aioodbc

    async def _ping() -> None:
        conn = await aioodbc.connect(
            dsn=settings.mssql_odbc_conn_str,
            timeout=int(settings.health_check_timeout_s),
        )
        try:
            cur = await conn.cursor()
            await cur.execute("SELECT 1")
            await cur.fetchone()
            await cur.close()
        finally:
            await conn.close()

    await asyncio.wait_for(_ping(), timeout=settings.health_check_timeout_s)
