"""
Pytest configuration and shared fixtures for all tests
"""

import pytest
import asyncio
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

# Test database URL - use SQLite for testing
TEST_DATABASE_URL = "sqlite:///:memory:"
TEST_DATABASE_ASYNC_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
async def async_session():
    """Create async session for database tests"""
    engine = create_async_engine(
        TEST_DATABASE_ASYNC_URL,
        echo=False,
        future=True
    )

    async_session_maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )

    # Import Base here to avoid circular imports
    from models.database import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_maker() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.fixture(scope="function")
def sync_session():
    """Create sync session for database tests"""
    from sqlalchemy import create_engine
    from models.database import Base

    engine = create_engine("sqlite:///:memory:")

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


# Sample test data fixtures
@pytest.fixture
def sample_building_with_bin():
    """Sample building with valid BIN"""
    return {
        'bin': '1001234',
        'bbl': '1000001',
        'address': '123 Main Street, New York, NY',
        'borough': 'Manhattan',
        'latitude': 40.7128,
        'longitude': -74.0060,
        'year_built': 1920,
        'num_floors': 10,
        'building_class': 'C1',
        'is_landmark': False,
        'scan_enabled': True
    }


@pytest.fixture
def sample_building_public_space():
    """Sample public space with 'N/A' BIN"""
    return {
        'bin': 'N/A',
        'bbl': '1000002',
        'address': 'Central Park, New York, NY',
        'borough': 'Manhattan',
        'latitude': 40.7829,
        'longitude': -73.9654,
        'year_built': None,
        'num_floors': None,
        'building_class': None,
        'is_landmark': False,
        'scan_enabled': False
    }


@pytest.fixture
def sample_landmark():
    """Sample landmark building"""
    return {
        'bin': '1002456',
        'bbl': '1000003',
        'address': 'Empire State Building, New York, NY',
        'borough': 'Manhattan',
        'latitude': 40.7484,
        'longitude': -73.9857,
        'year_built': 1931,
        'num_floors': 102,
        'building_class': 'O1',
        'is_landmark': True,
        'landmark_name': 'Empire State Building',
        'architect': 'Shreve, Lamb & Harmon',
        'architectural_style': 'Art Deco',
        'scan_enabled': True
    }


@pytest.fixture
def sample_reference_image():
    """Sample reference image"""
    return {
        'bin': '1001234',
        'image_url': 'https://r2.example.com/reference/1001234/90.jpg',
        'source': 'street_view',
        'compass_bearing': 90.0,
        'capture_lat': 40.7128,
        'capture_lng': -74.0060,
        'distance_from_building': 5.0,
        'quality_score': 0.95,
        'resolution_width': 1024,
        'resolution_height': 768,
        'is_verified': True
    }


@pytest.fixture
def sample_scan():
    """Sample scan record"""
    return {
        'id': 'scan-001',
        'gps_lat': 40.7128,
        'gps_lng': -74.0060,
        'compass_bearing': 90.0,
        'phone_pitch': 0.0,
        'phone_roll': 0.0,
        'candidate_bins': ['1001234', '1001235', '1001236'],
        'top_match_bin': '1001234',
        'top_confidence': 0.92,
        'confirmed_bin': None
    }
