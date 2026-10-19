import logging
from contextlib import asynccontextmanager
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from notification_engine.app.core.config import settings

logger = logging.getLogger(__name__)
def build_engine() -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
    )


# ---------------------------------------------------------------------------
# Session factory — thin wrapper so callers never touch sessionmaker directly
# ---------------------------------------------------------------------------

def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


# ---------------------------------------------------------------------------
# Async context manager — the ONLY sanctioned way to acquire a session
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_session():
    async with AsyncSessionLocal() as session:
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

async def verify_connectivity(engine: AsyncEngine) -> None:
    
    try:
        async with engine.connect() as conn:  # type: AsyncConnection
            await conn.execute(text("SELECT 1"))
        logger.info("Database connectivity check passed.")
    except Exception as exc:
        logger.critical("Database connectivity check FAILED: %s", exc)
        raise