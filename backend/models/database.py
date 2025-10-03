"""
Database models for NYC Scan application.
These models are designed to work with the existing Supabase setup.
"""

from sqlalchemy import (
    Column, String, Integer, Float, Boolean,
    ARRAY, Text, TIMESTAMP, JSON, ForeignKey
)
from sqlalchemy.ext.declarative import declarative_base
from geoalchemy2 import Geometry
from datetime import datetime
import uuid

Base = declarative_base()


class Building(Base):
    """
    Main buildings table - uses buildings_full_merge_scanning (860k NYC buildings + landmark data)
    """
    __tablename__ = 'buildings_full_merge_scanning'

    # Primary identifiers
    bbl = Column(String(10), primary_key=True)
    bin = Column(String(7), index=True)
    address = Column(Text, nullable=False)
    borough = Column(String(20))

    # Geometry (PostGIS)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    geom = Column(Geometry('POINT', srid=4326), index=True)

    # Physical characteristics
    year_built = Column(Integer, nullable=True)
    num_floors = Column(Integer, nullable=True)
    building_class = Column(String(10), nullable=True)
    land_use = Column(String(10), nullable=True)

    # Landmark data
    is_landmark = Column(Boolean, default=False, index=True, nullable=True)
    landmark_name = Column(Text, nullable=True)
    architect = Column(Text, nullable=True)
    architectural_style = Column(Text, nullable=True)  # Note: called architectural_style in DB
    short_bio = Column(Text, nullable=True)

    # Walk optimization
    is_walk_optimized = Column(Boolean, default=False, nullable=True)
    walk_score = Column(Float, nullable=True)

    # Image matching metadata
    scan_enabled = Column(Boolean, default=True, nullable=True)
    has_reference_images = Column(Boolean, default=False, nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)


class ReferenceImage(Base):
    """
    Stores reference images (Street View, Mapillary, user-uploaded)
    for building facade matching
    """
    __tablename__ = 'reference_images'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bbl = Column(String(10), ForeignKey('buildings.bbl'), index=True, nullable=False)

    # Image storage
    image_url = Column(Text, nullable=False)
    thumbnail_url = Column(Text)

    # Image metadata
    source = Column(String(20), nullable=False)  # 'street_view', 'mapillary', 'user'
    compass_bearing = Column(Float)  # Direction camera is facing (0-360)
    capture_lat = Column(Float)  # Where photo was taken from
    capture_lng = Column(Float)
    distance_from_building = Column(Float)  # meters

    # Quality metrics
    quality_score = Column(Float, default=1.0)
    resolution_width = Column(Integer)
    resolution_height = Column(Integer)
    is_verified = Column(Boolean, default=False)

    # CLIP embedding (for faster matching)
    clip_embedding = Column(ARRAY(Float))
    embedding_model = Column(String(50))  # e.g., "ViT-B-32"

    # Timestamps
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)


class Scan(Base):
    """
    Stores user scans for analytics and improvement
    """
    __tablename__ = 'scans'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # User (nullable for anonymous scans)
    user_id = Column(String(36), index=True)

    # User photo
    user_photo_url = Column(Text, nullable=False)
    thumbnail_url = Column(Text)

    # GPS & sensor data
    gps_lat = Column(Float, nullable=False)
    gps_lng = Column(Float, nullable=False)
    gps_accuracy = Column(Float)
    compass_bearing = Column(Float, nullable=False)  # 0-360 degrees
    phone_pitch = Column(Float, default=0)  # -90 to 90 degrees
    phone_roll = Column(Float, default=0)

    # Matching results
    candidate_bbls = Column(ARRAY(Text))  # All candidates considered
    candidate_scores = Column(JSON)  # Map of bbl -> confidence
    top_match_bbl = Column(String(10))
    top_confidence = Column(Float)

    # User confirmation
    confirmed_bbl = Column(String(10), ForeignKey('buildings.bbl'), index=True)
    was_correct = Column(Boolean)  # Did top match equal confirmed?
    confirmation_time_ms = Column(Integer)  # Time to confirm

    # Performance metrics
    processing_time_ms = Column(Integer)
    num_candidates = Column(Integer)
    geospatial_query_ms = Column(Integer)
    image_fetch_ms = Column(Integer)
    clip_comparison_ms = Column(Integer)

    # Error handling
    error_message = Column(Text)
    error_type = Column(String(50))

    # Timestamps
    created_at = Column(TIMESTAMP, default=datetime.utcnow, index=True)
    confirmed_at = Column(TIMESTAMP)


class ScanFeedback(Base):
    """
    User feedback on scan results
    """
    __tablename__ = 'scan_feedback'

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(String(36), ForeignKey('scans.id'), index=True)

    # Feedback
    rating = Column(Integer)  # 1-5 stars
    feedback_text = Column(Text)
    feedback_type = Column(String(20))  # 'correct', 'incorrect', 'slow', 'no_match'

    # Timestamps
    created_at = Column(TIMESTAMP, default=datetime.utcnow)


class CacheStat(Base):
    """
    Tracks cache statistics for reference images
    """
    __tablename__ = 'cache_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Cache metrics
    date = Column(TIMESTAMP, index=True)
    total_images = Column(Integer)
    images_fetched_today = Column(Integer)
    cache_hit_rate = Column(Float)
    avg_fetch_time_ms = Column(Float)
    total_cost_usd = Column(Float)

    # Source breakdown
    street_view_count = Column(Integer)
    mapillary_count = Column(Integer)
    user_upload_count = Column(Integer)
