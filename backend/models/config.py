"""
Configuration management for NYC Scan backend
"""

from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Supabase
    supabase_url: str
    supabase_key: str
    supabase_service_key: Optional[str] = None

    # Database
    database_url: str
    footprints_db_url: Optional[str] = None  # Railway database for building footprints
    search_db_url: Optional[str] = None  # Dedicated pgvector DB for the semantic search index

    # Connection pool sizing for the two Railway databases. Tunable from the
    # Railway service variables so a saturated pool is a config change, not a
    # redeploy.
    #
    # These were hardcoded at pool_size=3, max_overflow=2 — five connections
    # total — and a SINGLE beta tester exhausted them, producing 30-second
    # QueuePool timeouts on /api/search/unified and /api/lore. The cause is that
    # one unified search fans out into several concurrent queries, so one user
    # request can hold more than one connection at a time; five was under one
    # user's working set, never mind twenty.
    #
    # 10 + 10 gives 20 per container, comfortably inside Railway Postgres's
    # default max_connections (100) even with a couple of containers running.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # Fail fast instead of hanging the request for half a minute. A queue wait
    # this long is a saturated pool, and the client has given up long before.
    db_pool_timeout: int = 10

    # Redis (optional - can be disabled for initial deployment)
    redis_url: Optional[str] = None

    # Cloudflare R2
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str = "building-images"
    r2_public_url: str

    # User Images R2 Bucket (separate from reference images)
    r2_user_images_bucket: str = "user-images"
    r2_user_images_public_url: Optional[str] = None

    # Optional APIs
    perplexity_api_key: Optional[str] = None

    # App Configuration
    env: str = "development"
    debug: bool = True
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Scan Configuration
    max_scan_distance_meters: int = 150
    cone_angle_degrees: int = 75  # Tighter cone for better precision
    max_candidates: int = 20
    confidence_threshold: float = 0.80
    landmark_boost_factor: float = 1.05
    proximity_boost_threshold: float = 30  # meters
    proximity_boost_factor: float = 1.10

    # Image Configuration
    reference_image_bearing_tolerance: int = 30  # degrees

    # Cache Configuration
    cache_ttl_seconds: int = 86400  # 24 hours
    precache_top_n_buildings: int = 5000
    precache_cardinal_directions: list = [0, 90, 180, 270]

    # Scan System Configuration (footprint-based)
    v2_ambiguity_threshold: float = 15.0
    v2_single_building_confidence: float = 95.0
    v2_clear_winner_confidence: float = 85.0
    v2_expanded_radius_max: float = 300.0
    v2_expanded_radius_step: float = 50.0

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # Ignore extra fields in .env


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance
    """
    return Settings()
