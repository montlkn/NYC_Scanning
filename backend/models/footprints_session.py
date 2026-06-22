"""
Async database session management for Railway footprints database
"""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable, Optional, TypeVar
import asyncio
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

    # Create async engine for Railway — small persistent pool to avoid
    # TCP handshake overhead on every scan within a warm container
    footprints_engine = create_async_engine(
        database_url,
        echo=settings.debug,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=300,  # Recycle connections every 5 min
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


T = TypeVar("T")

# Railway closes idle Postgres connections and occasionally drops one mid-flight.
# pool_pre_ping catches a dead connection BEFORE a query, but a connection that
# dies DURING a query still raises OperationalError/InterfaceError (or SQLAlchemy
# invalidates it) and bubbles a 500 to the scan client. This is the same failure
# class that timed out the landmark ingest. run_footprints_query retries the whole
# unit of work on those transient errors with a fresh session, so one stale
# connection self-heals instead of failing the request.
_TRANSIENT_DB_ERRORS = (OperationalError, InterfaceError)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, _TRANSIENT_DB_ERRORS):
        return True
    # SQLAlchemy wraps a dropped connection as DBAPIError with the flag set.
    return isinstance(exc, DBAPIError) and bool(getattr(exc, "connection_invalidated", False))


async def run_footprints_query(
    work: Callable[[AsyncSession], Awaitable[T]],
    *,
    retries: int = 2,
    default: Optional[T] = None,
) -> Optional[T]:
    """Run `work(session)` against the footprints DB, retrying transient
    connection drops with a fresh session. Returns `default` if the DB is not
    configured. Non-transient errors (bad SQL, etc.) are NOT retried — they raise.

        rows = await run_footprints_query(
            lambda db: db.execute(text("SELECT ...")),
        )
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        async with get_footprints_db() as db:
            if db is None:
                return default
            try:
                return await work(db)
            except Exception as exc:  # noqa: BLE001 — re-raised below if not transient
                if not _is_transient(exc) or attempt == retries:
                    raise
                last_exc = exc
                logger.warning(
                    f"[footprints] transient DB error (attempt {attempt + 1}/{retries + 1}), "
                    f"reconnecting: {exc}"
                )
        # Brief backoff before the reconnect attempt; the session is already
        # closed by get_footprints_db's context exit.
        await asyncio.sleep(0.25 * (attempt + 1))
    if last_exc:  # pragma: no cover — loop either returns or raises above
        raise last_exc
    return default


async def footprints_db_ok() -> bool:
    """Lightweight liveness probe for /health — does the footprints DB answer
    a trivial query? Never raises; returns False on any failure so the health
    endpoint can report `degraded` instead of lying `ok` or 500-ing."""
    try:
        from sqlalchemy import text

        async def _ping(db: AsyncSession):
            await db.execute(text("SELECT 1"))
            return True

        return bool(await run_footprints_query(_ping, retries=1, default=False))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[health] footprints DB ping failed: {e}")
        return False


async def close_footprints_db():
    """
    Close footprints database connections
    Called during application shutdown
    """
    if footprints_engine:
        await footprints_engine.dispose()
        logger.info("Footprints database connections closed")
