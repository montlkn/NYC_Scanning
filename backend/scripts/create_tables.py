#!/usr/bin/env python3
"""
Create scans and scan_feedback tables in the database.
Run this once to set up the analytics tables.
"""

import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine
from models.config import get_settings
from models.database import Base, Scan, ScanFeedback, CacheStat

async def create_tables():
    """Create all tables defined in models"""
    settings = get_settings()

    # Use psycopg3 — asyncpg fails auth with Supabase Session pooler
    database_url = settings.database_url
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    import ssl
    from sqlalchemy.pool import NullPool
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    engine = create_async_engine(
        database_url, echo=True, poolclass=NullPool,
        connect_args={"sslmode": "require"}
    )

    async with engine.begin() as conn:
        # Create all tables
        print("Creating tables...")
        await conn.run_sync(Base.metadata.create_all)
        print("✅ Tables created successfully!")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(create_tables())
