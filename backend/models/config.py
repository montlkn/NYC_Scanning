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

    # Redis (optional - can be disabled for initial deployment)
    redis_url: Optional[str] = None

    # Google Maps
    google_maps_api_key: str

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
    max_scan_distance_meters: int = 100
    cone_angle_degrees: int = 60
    max_candidates: int = 20
    confidence_threshold: float = 0.70
    landmark_boost_factor: float = 1.05
    proximity_boost_threshold: float = 30  # meters
    proximity_boost_factor: float = 1.10

    # Image Configuration
    reference_image_bearing_tolerance: int = 30  # degrees
    street_view_size: str = "600x600"
    street_view_pitch: int = 10
    street_view_fov: int = 60

    # CLIP Model Configuration
    clip_model_name: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"
    clip_device: str = "cuda"  # or "cpu"

    # Cache Configuration
    cache_ttl_seconds: int = 86400  # 24 hours
    precache_top_n_buildings: int = 5000
    precache_cardinal_directions: list = [0, 90, 180, 270]

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
