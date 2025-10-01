"""
Async database session management for Supabase PostgreSQL
"""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from contextlib import asynccontextmanager
from typing import AsyncGenerator
import logging
import ssl

from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Convert postgres:// to postgresql+asyncpg://
# Remove any existing query parameters for clean parsing
database_url = settings.database_url.split('?')[0]
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

# Create SSL context that requires encryption for Supabase
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False  # Supabase uses shared certificates
ssl_context.verify_mode = ssl.CERT_NONE  # Don't verify certificate (Supabase pooled connections)

# Create async engine with SSL required for Supabase
engine = create_async_engine(
    database_url,
    echo=settings.debug,  # Log SQL queries in debug mode
    pool_pre_ping=True,   # Verify connections before using
    poolclass=NullPool if settings.env == "development" else None,  # No pooling in dev
    connect_args={
        "ssl": ssl_context,  # Pass SSL context to require encrypted connection
        "server_settings": {
            "application_name": "nyc_scan_backend"
        }
    }
)

# Create session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for FastAPI endpoints to get database session

    Usage:
        @app.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Item))
            return result.scalars().all()
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.error(f"Database session error: {e}", exc_info=True)
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager for database sessions (for use in scripts/services)

    Usage:
        async with get_db_context() as db:
            result = await db.execute(select(Building))
            buildings = result.scalars().all()
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"Database context error: {e}", exc_info=True)
            raise
        finally:
            await session.close()


async def init_db():
    """
    Initialize database connection
    Called during application startup
    """
    try:
        async with engine.begin() as conn:
            # Test connection
            await conn.execute(text("SELECT 1"))
            logger.info("✅ Database connection successful")

            # Verify PostGIS is installed
            result = await conn.execute(
                text("SELECT PostGIS_version()")
            )
            postgis_version = result.scalar()
            logger.info(f"✅ PostGIS version: {postgis_version}")

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        logger.warning("⚠️  Continuing without database connection - API endpoints requiring database will fail")
        # Don't raise - allow server to start without database for development


async def close_db():
    """
    Close database connections
    Called during application shutdown
    """
    await engine.dispose()
    logger.info("Database connections closed")


# Import text for raw SQL queries
from sqlalchemy import text
