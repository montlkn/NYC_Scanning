-- NYC Scan Backend - Database Migration 001
-- Adds tables for scan functionality while preserving existing buildings_duplicate table
-- Run this on your Supabase database

-- Enable PostGIS if not already enabled
CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================================================
-- REFERENCE IMAGES TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.reference_images (
    id SERIAL PRIMARY KEY,
    bbl VARCHAR(10) NOT NULL,
    image_url TEXT NOT NULL,
    thumbnail_url TEXT,
    source VARCHAR(20) NOT NULL,
    compass_bearing FLOAT,
    capture_lat FLOAT,
    capture_lng FLOAT,
    distance_from_building FLOAT,
    quality_score FLOAT DEFAULT 1.0,
    resolution_width INTEGER,
    resolution_height INTEGER,
    is_verified BOOLEAN DEFAULT FALSE,
    clip_embedding FLOAT[],
    embedding_model VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT fk_reference_images_bbl
        FOREIGN KEY (bbl)
        REFERENCES buildings_duplicate(bbl)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reference_images_bbl ON public.reference_images(bbl);
CREATE INDEX IF NOT EXISTS idx_reference_images_source ON public.reference_images(source);
CREATE INDEX IF NOT EXISTS idx_reference_images_bearing ON public.reference_images(compass_bearing);
CREATE INDEX IF NOT EXISTS idx_reference_images_quality ON public.reference_images(quality_score DESC);

-- ============================================================================
-- SCANS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.scans (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36),
    user_photo_url TEXT NOT NULL,
    thumbnail_url TEXT,
    gps_lat FLOAT NOT NULL,
    gps_lng FLOAT NOT NULL,
    gps_accuracy FLOAT,
    compass_bearing FLOAT NOT NULL,
    phone_pitch FLOAT DEFAULT 0,
    phone_roll FLOAT DEFAULT 0,
    candidate_bbls TEXT[],
    candidate_scores JSONB,
    top_match_bbl VARCHAR(10),
    top_confidence FLOAT,
    confirmed_bbl VARCHAR(10),
    was_correct BOOLEAN,
    confirmation_time_ms INTEGER,
    confirmed_at TIMESTAMPTZ,
    processing_time_ms INTEGER,
    num_candidates INTEGER,
    geospatial_query_ms INTEGER,
    image_fetch_ms INTEGER,
    clip_comparison_ms INTEGER,
    error_message TEXT,
    error_type VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT fk_scans_confirmed_bbl
        FOREIGN KEY (confirmed_bbl)
        REFERENCES buildings_duplicate(bbl)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_scans_user_id ON public.scans(user_id);
CREATE INDEX IF NOT EXISTS idx_scans_created_at ON public.scans(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scans_confirmed_bbl ON public.scans(confirmed_bbl);
CREATE INDEX IF NOT EXISTS idx_scans_was_correct ON public.scans(was_correct);

ALTER TABLE public.scans ADD COLUMN IF NOT EXISTS geom GEOMETRY(POINT, 4326);
CREATE INDEX IF NOT EXISTS idx_scans_geom ON public.scans USING GIST(geom);

CREATE OR REPLACE FUNCTION update_scan_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom = ST_SetSRID(ST_MakePoint(NEW.gps_lng, NEW.gps_lat), 4326);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_scan_geom ON public.scans;
CREATE TRIGGER trigger_update_scan_geom
    BEFORE INSERT OR UPDATE ON public.scans
    FOR EACH ROW
    EXECUTE FUNCTION update_scan_geom();

-- ============================================================================
-- SCAN FEEDBACK TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.scan_feedback (
    id SERIAL PRIMARY KEY,
    scan_id VARCHAR(36) NOT NULL,
    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
    feedback_text TEXT,
    feedback_type VARCHAR(20),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT fk_scan_feedback_scan_id
        FOREIGN KEY (scan_id)
        REFERENCES scans(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scan_feedback_scan_id ON public.scan_feedback(scan_id);
CREATE INDEX IF NOT EXISTS idx_scan_feedback_type ON public.scan_feedback(feedback_type);

-- ============================================================================
-- CACHE STATS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.cache_stats (
    id SERIAL PRIMARY KEY,
    date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_images INTEGER DEFAULT 0,
    images_fetched_today INTEGER DEFAULT 0,
    cache_hit_rate FLOAT,
    avg_fetch_time_ms FLOAT,
    total_cost_usd FLOAT DEFAULT 0,
    street_view_count INTEGER DEFAULT 0,
    mapillary_count INTEGER DEFAULT 0,
    user_upload_count INTEGER DEFAULT 0,
    UNIQUE(date)
);

CREATE INDEX IF NOT EXISTS idx_cache_stats_date ON public.cache_stats(date DESC);

-- ============================================================================
-- UPDATE BUILDINGS_DUPLICATE TABLE
-- ============================================================================

ALTER TABLE public.buildings_duplicate
ADD COLUMN IF NOT EXISTS scan_enabled BOOLEAN DEFAULT TRUE;

ALTER TABLE public.buildings_duplicate
ADD COLUMN IF NOT EXISTS has_reference_images BOOLEAN DEFAULT FALSE;

ALTER TABLE public.buildings_duplicate
ADD COLUMN IF NOT EXISTS short_bio TEXT;

CREATE INDEX IF NOT EXISTS idx_buildings_duplicate_scan_enabled 
ON public.buildings_duplicate(scan_enabled) WHERE scan_enabled = TRUE;

-- ============================================================================
-- ANALYTICS VIEWS
-- ============================================================================

CREATE OR REPLACE VIEW public.scan_accuracy_by_building AS
SELECT
    confirmed_bbl as bbl,
    COUNT(*) as total_scans,
    SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct_matches,
    ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / COUNT(*), 2) as accuracy_percent,
    AVG(top_confidence) as avg_confidence,
    AVG(processing_time_ms) as avg_processing_time_ms
FROM public.scans
WHERE confirmed_bbl IS NOT NULL
GROUP BY confirmed_bbl;

CREATE OR REPLACE VIEW public.daily_scan_stats AS
SELECT
    DATE(created_at) as scan_date,
    COUNT(*) as total_scans,
    COUNT(DISTINCT user_id) as unique_users,
    SUM(CASE WHEN confirmed_bbl IS NOT NULL THEN 1 ELSE 0 END) as confirmed_scans,
    SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct_matches,
    ROUND(AVG(processing_time_ms), 0) as avg_processing_time_ms,
    ROUND(AVG(top_confidence), 3) as avg_confidence
FROM public.scans
GROUP BY DATE(created_at)
ORDER BY scan_date DESC;

CREATE OR REPLACE VIEW public.buildings_needing_images AS
SELECT
    b.bbl,
    b.address,
    b.borough,
    b.is_landmark,
    b.final_score,
    COUNT(ri.id) as image_count
FROM public.buildings_duplicate b
LEFT JOIN public.reference_images ri ON b.bbl = ri.bbl
WHERE b.scan_enabled = TRUE
GROUP BY b.bbl, b.address, b.borough, b.is_landmark, b.final_score
HAVING COUNT(ri.id) < 4
ORDER BY b.final_score DESC NULLS LAST;

-- ============================================================================
-- RLS + FUNCTIONS + GRANTS (same, but target buildings_duplicate)
-- ============================================================================

ALTER TABLE public.scans ENABLE ROW LEVEL SECURITY;
-- (policies unchanged…)

ALTER TABLE public.scan_feedback ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.reference_images ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.get_nearby_scans(
    p_lat FLOAT,
    p_lng FLOAT,
    p_radius_meters INTEGER DEFAULT 1000
)
RETURNS TABLE (
    scan_id VARCHAR,
    distance_meters FLOAT,
    confirmed_bbl VARCHAR,
    was_correct BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.id,
        ST_Distance(
            s.geom::geography,
            ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326)::geography
        ) as distance_meters,
        s.confirmed_bbl,
        s.was_correct
    FROM public.scans s
    WHERE ST_DWithin(
        s.geom::geography,
        ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326)::geography,
        p_radius_meters
    )
    ORDER BY distance_meters;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_building_reference_flags()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE public.buildings_duplicate
    SET has_reference_images = EXISTS (
        SELECT 1
        FROM public.reference_images
        WHERE reference_images.bbl = buildings_duplicate.bbl
    );
END;
$$;

GRANT SELECT ON public.reference_images TO authenticated;
GRANT SELECT ON public.buildings_duplicate TO authenticated;
GRANT SELECT, INSERT, UPDATE ON public.scans TO authenticated;
GRANT SELECT, INSERT ON public.scan_feedback TO authenticated;

GRANT SELECT ON public.scan_accuracy_by_building TO authenticated;
GRANT SELECT ON public.daily_scan_stats TO authenticated;

GRANT EXECUTE ON FUNCTION public.get_nearby_scans TO authenticated;

SELECT public.update_building_reference_flags();

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 001 (buildings_duplicate) completed successfully!';
END $$;
