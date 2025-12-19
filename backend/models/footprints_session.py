"""
Async database session management for Railway footprints database
"""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
import logging

from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Initialize as None - will be created only if footprints_db_url is configured
footprints_engine = None
FootprintsSessionLocal = None


def init_footprints_engine():
    """Initialize the footprints database engine if configured"""
    global footprints_engine, FootprintsSessionLocal

    if not settings.footprints_db_url:
        logger.warning("FOOTPRINTS_DB_URL not configured - footprint queries will fall back to V1")
        return None

    # Convert postgres:// to postgresql+psycopg://
    database_url = settings.footprints_db_url.split('?')[0]
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    # Create async engine for Railway
    footprints_engine = create_async_engine(
        database_url,
        echo=settings.debug,
        pool_pre_ping=True,
        poolclass=NullPool,  # Railway handles pooling
        connect_args={
            "options": "-c application_name=nyc_scan_footprints"
        }
    )

    # Create session factory
    FootprintsSessionLocal = async_sessionmaker(
        footprints_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    logger.info("✅ Footprints database engine initialized (Railway)")
    return footprints_engine


@asynccontextmanager
async def get_footprints_db() -> AsyncGenerator[Optional[AsyncSession], None]:
    """
    Get a database session for the footprints database

    Returns None if footprints database is not configured

    Usage:
        async with get_footprints_db() as db:
            if db:
                result = await db.execute(text("SELECT ..."))
    """
    if FootprintsSessionLocal is None:
        init_footprints_engine()

    if FootprintsSessionLocal is None:
        # Not configured - yield None
        yield None
        return

    async with FootprintsSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.error(f"Footprints database session error: {e}", exc_info=True)
            raise
        finally:
            await session.close()


async def close_footprints_db():
    """
    Close footprints database connections
    Called during application shutdown
    """
    if footprints_engine:
        await footprints_engine.dispose()
        logger.info("Footprints database connections closed")
