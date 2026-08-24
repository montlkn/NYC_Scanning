"""
Async session for the dedicated semantic-search Postgres (pgvector).

Separate from the PostGIS footprints DB (models/footprints_session.py) so the
search index can live on a pgvector/pgvector image without touching prod
PostGIS. Pointed at by SEARCH_DB_URL.
"""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
import logging

from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

search_engine = None
SearchSessionLocal = None


def init_search_engine():
    """Initialize the search database engine if SEARCH_DB_URL is configured."""
    global search_engine, SearchSessionLocal

    if not settings.search_db_url:
        logger.warning("SEARCH_DB_URL not configured - /search will be unavailable")
        return None

    database_url = settings.search_db_url.split("?")[0]
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    search_engine = create_async_engine(
        database_url,
        echo=settings.debug,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=300,
        # hnsw.ef_search is the HNSW candidate list size, and pgvector defaults
        # it to 40. That is a CEILING on rows returned, not just a recall knob:
        # every semantic query came back with exactly 40 results no matter what
        # LIMIT asked for, against a 225,810-row venue corpus. A `limit=100`
        # search was silently answering with 40, and which 40 depended on where
        # the query landed in the graph.
        #
        # Set on the connection rather than per query so no vector search can
        # forget it. 200 comfortably exceeds the largest LIMIT the API allows
        # (100); pgvector's own guidance is that ef_search must be at least the
        # LIMIT you intend to serve.
        connect_args={
            "options": "-c application_name=nyc_scan_search -c hnsw.ef_search=200"
        },
    )
    SearchSessionLocal = async_sessionmaker(
        search_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    logger.info("✅ Search database engine initialized (pgvector)")
    return search_engine


@asynccontextmanager
async def get_search_db() -> AsyncGenerator[Optional[AsyncSession], None]:
    """Yield a search-DB session, or None if SEARCH_DB_URL is not configured."""
    if SearchSessionLocal is None:
        init_search_engine()

    if SearchSessionLocal is None:
        yield None
        return

    async with SearchSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.error(f"Search database session error: {e}", exc_info=True)
            raise
        finally:
            await session.close()


async def close_search_db():
    if search_engine:
        await search_engine.dispose()
        logger.info("Search database connections closed")
