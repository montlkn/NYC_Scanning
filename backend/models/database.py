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
    Now uses BIN (Building Identification Number) as primary key instead of BBL.
    This allows proper handling of multiple buildings on the same lot (BBL).
    """
    __tablename__ = 'buildings_full_merge_scanning'

    # Primary identifiers - note: using 'id' as primary key since BIN/BBL are stored as text with decimals
    id = Column(Integer, primary_key=True, autoincrement=True)
    bin = Column(Text, index=True, nullable=True)  # BIN as text (may have .0 suffix)
    bbl = Column(Text, index=True, nullable=True)  # BBL as text (may have .0 suffix)
    address = Column(Text, nullable=True)
    borough = Column(Text, nullable=True)

    # Coordinates (stored as text in database, cast to float when querying)
    # Map to actual column names in the database
    latitude = Column('geocoded_lat', Text, nullable=True)
    longitude = Column('geocoded_lng', Text, nullable=True)


class ReferenceImage(Base):
    """
    Stores reference images (Street View, Mapillary, user-uploaded)
    for building facade matching
    Now uses BIN (Building Identification Number) as foreign key instead of BBL
    """
    __tablename__ = 'reference_images'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bin = Column(String(10), ForeignKey('buildings_full_merge_scanning.bin'), index=True, nullable=False)

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

    # Matching results - now using BIN instead of BBL
    candidate_bins = Column(ARRAY(Text))  # All candidates considered (BINs instead of BBLs)
    candidate_scores = Column(JSON)  # Map of bin -> confidence
    top_match_bin = Column(String(10))
    top_confidence = Column(Float)

    # User confirmation
    confirmed_bin = Column(String(10), ForeignKey('buildings_full_merge_scanning.bin'), index=True)
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


class UserContributedBuilding(Base):
    """
    User-contributed building metadata and enrichment data.
    Allows crowdsourcing additional information for buildings in NYC datasets.
    """
    __tablename__ = 'user_contributed_buildings'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Link to NYC building data
    bin = Column(String(10), nullable=False, index=True)
    bbl = Column(String(10), index=True)
    building_id = Column(Integer, index=True)  # FK to buildings_full_merge_scanning

    # Address and names
    address = Column(Text, nullable=False)
    building_name = Column(Text)
    alternate_names = Column(ARRAY(Text))

    # Location from user's scan
    gps_lat = Column(Float, nullable=False)
    gps_lng = Column(Float, nullable=False)
    gps_accuracy = Column(Float)

    # Building metadata
    year_built = Column(Integer)
    architect = Column(Text)
    architectural_style = Column(Text)
    num_floors = Column(Integer)
    height_feet = Column(Float)
    landmark_status = Column(Text)
    historic_district = Column(Text)
    building_use = Column(Text)
    notable_features = Column(Text)
    user_notes = Column(Text)

    # Submission data
    submitted_by = Column(String(36), index=True)
    initial_photo_url = Column(Text, nullable=False)
    initial_scan_id = Column(String(36), ForeignKey('scans.id'))
    compass_bearing = Column(Float)
    phone_pitch = Column(Float)

    # Verification status
    status = Column(String(20), default='pending', index=True)
    verified_by = Column(String(36))
    verified_at = Column(TIMESTAMP)
    rejection_reason = Column(Text)

    # Data enrichment
    enrichment_status = Column(String(20), default='pending', index=True)
    enrichment_source = Column(Text)
    enrichment_data = Column(JSON)
    enrichment_confidence = Column(Float)
    enrichment_completed_at = Column(TIMESTAMP)

    # Street View images
    street_view_images_fetched = Column(Boolean, default=False)
    street_view_image_count = Column(Integer, default=0)
    street_view_fetch_attempted_at = Column(TIMESTAMP)

    # Reference images
    reference_image_count = Column(Integer, default=0)

    # Community validation
    upvotes = Column(Integer, default=0)
    downvotes = Column(Integer, default=0)

    # Timestamps
    created_at = Column(TIMESTAMP, default=datetime.utcnow, index=True)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)
