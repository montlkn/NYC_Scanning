from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
import os
from functools import lru_cache
from dotenv import load_dotenv

# Load environment variables from .env if available (for local dev)
load_dotenv()

# Lazy initialization - no global engine/session creation
_engine = None
_SessionLocal = None


@lru_cache()
def get_engine():
    """
    Lazy-initialize the database engine.
    This only runs when first called, not at import time.
    """
    global _engine
    if _engine is None:
        scan_db_url = os.getenv('SCAN_DB_URL')

        if not scan_db_url:
            raise RuntimeError(
                "SCAN_DB_URL environment variable is not set. "
                "Please check your .env file or Modal secrets."
            )

        # Use NullPool for better compatibility with Supabase pooler
        _engine = create_engine(
            scan_db_url,
            poolclass=NullPool,
            echo=False,
            connect_args={"connect_timeout": 10}
        )

    return _engine


def get_session_local():
    """
    Get the SessionLocal class, creating it if needed.
    """
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    return _SessionLocal


def get_scan_db():
    """
    FastAPI dependency for database sessions.
    This is called per-request, not at import time.
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
