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

    # Create async engine
    # Convert postgresql:// to postgresql+asyncpg:// for async connection
    database_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(database_url, echo=True)

    async with engine.begin() as conn:
        # Create all tables
        print("Creating tables...")
        await conn.run_sync(Base.metadata.create_all)
        print("✅ Tables created successfully!")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(create_tables())
