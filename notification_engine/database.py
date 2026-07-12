"""
database.py — Async SQLAlchemy 2.0 engine and session lifecycle management.

Design contract:
  - One AsyncEngine per process, created at startup, disposed at shutdown.
  - Every database interaction MUST obtain a session via `get_session()`.
  - Sessions are never leaked: the async context manager guarantees rollback
    and close even when the caller raises mid-transaction.
  - Connection pool tuning is centralised here so Ops can adjust without
    touching business logic.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ORM base — all models inherit from this single source of truth
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Project-wide declarative base for all ORM models."""


# ---------------------------------------------------------------------------
# Engine factory — called once during lifespan startup
# ---------------------------------------------------------------------------

def build_engine() -> AsyncEngine:
    """
    Construct a production-grade AsyncEngine backed by asyncpg.

    Pool parameters are conservative defaults for a single-node API server.
    Tune `pool_size` and `max_overflow` to match your connection budget on
    the target PostgreSQL instance.
    """
    engine = create_async_engine(
        settings.database_url,          # must start with postgresql+asyncpg://
        echo=settings.db_echo,          # set False in production
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=30,                # seconds to wait for a free connection
        pool_recycle=1800,              # recycle connections after 30 min
        pool_pre_ping=True,             # evict stale connections before use
        connect_args={
            "server_settings": {
                "application_name": "notification_engine",
            },
            "command_timeout": 10,      # asyncpg per-query hard timeout (s)
        },
    )

    # Emit a structured log entry whenever a new physical connection opens
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, connection_record) -> None:  # type: ignore[type-arg]
        logger.debug("New DB connection established: %s", connection_record)

    return engine


# ---------------------------------------------------------------------------
# Session factory — thin wrapper so callers never touch sessionmaker directly
# ---------------------------------------------------------------------------

def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """
    Return a configured async_sessionmaker bound to *engine*.

    `expire_on_commit=False` prevents lazy-load surprises in async code where
    the session is already closed by the time the response serialiser runs.
    `autoflush=False` gives the service layer explicit control over flush timing.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# Async context manager — the ONLY sanctioned way to acquire a session
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """
    Yield a transactional AsyncSession and guarantee cleanup.

    Usage::

        async with get_session(session_factory) as session:
            result = await session.execute(select(Notification))

    On success  → commits and closes.
    On exception → rolls back, closes, and re-raises so FastAPI can return 500.
    """
    session: AsyncSession = factory()
    try:
        yield session
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.exception("DB transaction rolled back due to SQLAlchemy error: %s", exc)
        raise
    except Exception as exc:
        await session.rollback()
        logger.exception("DB transaction rolled back due to unexpected error: %s", exc)
        raise
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Schema bootstrap helper — used in tests / first-run migrations fallback
# ---------------------------------------------------------------------------

async def create_all_tables(engine: AsyncEngine) -> None:
    """
    Create all tables registered on `Base.metadata`.

    In production you should use Alembic instead; this helper exists for
    integration tests and local development bootstrapping only.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("All ORM tables verified / created.")


async def verify_connectivity(engine: AsyncEngine) -> None:
    """
    Raise immediately on startup if the database is unreachable.

    Lets Kubernetes / the process supervisor restart the pod before traffic
    is routed to a dead instance.
    """
    try:
        async with engine.connect() as conn:  # type: AsyncConnection
            await conn.execute(text("SELECT 1"))
        logger.info("Database connectivity check passed.")
    except Exception as exc:
        logger.critical("Database connectivity check FAILED: %s", exc)
        raise