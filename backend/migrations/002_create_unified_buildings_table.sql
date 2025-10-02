-- NYC Scan Backend - Database Migration 002
-- Creates unified buildings table combining PLUTO + Landmarks data
-- Run this on your Supabase database AFTER migration 001

-- ============================================================================
-- UNIFIED BUILDINGS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.buildings_full_merge_scanning (
    -- Primary Key
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identifiers
    bbl VARCHAR(10) UNIQUE NOT NULL,                    -- NYC BBL (Borough-Block-Lot)
    bin VARCHAR(7),                                      -- Building Identification Number

    -- Location
    address TEXT NOT NULL,
    borough VARCHAR(20) NOT NULL,                        -- Manhattan, Brooklyn, Queens, Bronx, Staten Island
    zip_code VARCHAR(5),
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(POINT, 4326),                          -- PostGIS geometry for spatial queries

    -- Physical Attributes (from PLUTO)
    num_floors INTEGER,                                  -- KEY: For barometer floor validation
    year_built INTEGER,
    building_class VARCHAR(10),                          -- PLUTO building class code
    land_use VARCHAR(10),                               -- Residential/Commercial/etc
    lot_area DOUBLE PRECISION,                          -- Square feet
    building_area DOUBLE PRECISION,                     -- Square feet

    -- Landmark Data
    is_landmark BOOLEAN DEFAULT FALSE,
    landmark_name TEXT,                                 -- Official landmark name
    lpc_number VARCHAR(20),                             -- Landmarks Preservation Commission number
    designation_date DATE,

    -- Architectural Details
    architect TEXT,
    architectural_style TEXT,
    historic_period TEXT,
    short_bio TEXT,                                     -- Short description for UI

    -- Scoring & Filtering
    landmark_score DOUBLE PRECISION,                    -- Your custom scoring
    final_score DOUBLE PRECISION,                       -- Combined score for prioritization
    scan_enabled BOOLEAN DEFAULT TRUE,                  -- Enable/disable for scanning
    has_reference_images BOOLEAN DEFAULT FALSE,         -- Auto-updated by trigger

    -- Metadata
    data_source TEXT[],                                 -- ['pluto', 'landmarks', 'manual']
    data_quality_score DOUBLE PRECISION DEFAULT 1.0,
    last_verified TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- INDEXES
-- ============================================================================

-- Spatial index (most important for cone-of-vision queries)
CREATE INDEX idx_buildings_geom ON public.buildings_full_merge_scanning USING GIST(geom);

-- BBL lookup (primary key for joins)
CREATE INDEX idx_buildings_bbl ON public.buildings_full_merge_scanning(bbl);

-- Scan filtering
CREATE INDEX idx_buildings_scan_enabled ON public.buildings_full_merge_scanning(scan_enabled) WHERE scan_enabled = TRUE;
CREATE INDEX idx_buildings_has_images ON public.buildings_full_merge_scanning(has_reference_images) WHERE has_reference_images = TRUE;

-- Landmark filtering
CREATE INDEX idx_buildings_is_landmark ON public.buildings_full_merge_scanning(is_landmark) WHERE is_landmark = TRUE;

-- Scoring (for prioritization)
CREATE INDEX idx_buildings_final_score ON public.buildings_full_merge_scanning(final_score DESC NULLS LAST);

-- Borough filtering
CREATE INDEX idx_buildings_borough ON public.buildings_full_merge_scanning(borough);

-- Full text search on address
CREATE INDEX idx_buildings_address_search ON public.buildings_full_merge_scanning USING gin(to_tsvector('english', address));

-- ============================================================================
-- TRIGGERS
-- ============================================================================

-- Auto-update geometry from lat/lng
CREATE OR REPLACE FUNCTION update_building_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom = ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_building_geom ON public.buildings_full_merge_scanning;
CREATE TRIGGER trigger_update_building_geom
    BEFORE INSERT OR UPDATE OF latitude, longitude ON public.buildings_full_merge_scanning
    FOR EACH ROW
    EXECUTE FUNCTION update_building_geom();

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_buildings_updated_at ON public.buildings_full_merge_scanning;
CREATE TRIGGER trigger_update_buildings_updated_at
    BEFORE UPDATE ON public.buildings_full_merge_scanning
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- ============================================================================
-- UPDATE EXISTING FOREIGN KEYS
-- ============================================================================

-- Update reference_images to point to new buildings table
ALTER TABLE public.reference_images
DROP CONSTRAINT IF EXISTS fk_reference_images_bbl;

ALTER TABLE public.reference_images
ADD CONSTRAINT fk_reference_images_bbl
    FOREIGN KEY (bbl)
    REFERENCES buildings(bbl)
    ON DELETE CASCADE;

-- Update scans to point to new buildings table
ALTER TABLE public.scans
DROP CONSTRAINT IF EXISTS fk_scans_confirmed_bbl;

ALTER TABLE public.scans
ADD CONSTRAINT fk_scans_confirmed_bbl
    FOREIGN KEY (confirmed_bbl)
    REFERENCES buildings(bbl)
    ON DELETE SET NULL;

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Get buildings within cone of vision (used by scan endpoint)
CREATE OR REPLACE FUNCTION public.get_buildings_in_cone(
    p_lat DOUBLE PRECISION,
    p_lng DOUBLE PRECISION,
    p_bearing DOUBLE PRECISION,
    p_distance_meters INTEGER DEFAULT 100,
    p_cone_angle_degrees DOUBLE PRECISION DEFAULT 45
)
RETURNS TABLE (
    bbl VARCHAR,
    address TEXT,
    distance_meters DOUBLE PRECISION,
    bearing_to_building DOUBLE PRECISION,
    num_floors INTEGER,
    is_landmark BOOLEAN
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        b.bbl,
        b.address,
        ST_Distance(
            b.geom::geography,
            ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326)::geography
        ) as distance_meters,
        degrees(ST_Azimuth(
            ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326),
            b.geom
        )) as bearing_to_building,
        b.num_floors,
        b.is_landmark
    FROM public.buildings_full_merge_scanning b
    WHERE
        b.scan_enabled = TRUE
        AND ST_DWithin(
            b.geom::geography,
            ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326)::geography,
            p_distance_meters
        )
        AND abs(
            ((degrees(ST_Azimuth(
                ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326),
                b.geom
            )) - p_bearing + 540) % 360) - 180
        ) <= p_cone_angle_degrees
    ORDER BY distance_meters;
END;
$$;

-- Update has_reference_images flags
CREATE OR REPLACE FUNCTION public.update_building_reference_flags()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE public.buildings_full_merge_scanning
    SET has_reference_images = EXISTS (
        SELECT 1
        FROM public.reference_images
        WHERE reference_images.bbl = buildings_full_merge_scanning.bbl
    );
END;
$$;

-- ============================================================================
-- UPDATE ANALYTICS VIEWS
-- ============================================================================

-- Replace view to use new buildings table
DROP VIEW IF EXISTS public.buildings_full_merge_scanning_needing_images;
CREATE OR REPLACE VIEW public.buildings_full_merge_scanning_needing_images AS
SELECT
    b.bbl,
    b.address,
    b.borough,
    b.is_landmark,
    b.final_score,
    COUNT(ri.id) as image_count
FROM public.buildings_full_merge_scanning b
LEFT JOIN public.reference_images ri ON b.bbl = ri.bbl
WHERE b.scan_enabled = TRUE
GROUP BY b.bbl, b.address, b.borough, b.is_landmark, b.final_score
HAVING COUNT(ri.id) < 4
ORDER BY b.final_score DESC NULLS LAST;

-- ============================================================================
-- ROW LEVEL SECURITY
-- ============================================================================

ALTER TABLE public.buildings_full_merge_scanning ENABLE ROW LEVEL SECURITY;

-- Public read access (all users can query buildings)
DROP POLICY IF EXISTS "Buildings are viewable by everyone" ON public.buildings_full_merge_scanning;
CREATE POLICY "Buildings are viewable by everyone"
    ON public.buildings_full_merge_scanning FOR SELECT
    USING (true);

-- ============================================================================
-- GRANTS
-- ============================================================================

GRANT SELECT ON public.buildings_full_merge_scanning TO authenticated;
GRANT SELECT ON public.buildings_full_merge_scanning TO anon;
GRANT EXECUTE ON FUNCTION public.get_buildings_in_cone TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_buildings_in_cone TO anon;

-- ============================================================================
-- MIGRATION NOTES
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 002 completed successfully!';
    RAISE NOTICE '';
    RAISE NOTICE '📊 Next steps:';
    RAISE NOTICE '1. Run backend/scripts/ingest_pluto.py to load PLUTO data';
    RAISE NOTICE '2. Run backend/scripts/enrich_landmarks.py to add landmark scores';
    RAISE NOTICE '3. Run SELECT public.update_building_reference_flags() to update image flags';
    RAISE NOTICE '';
    RAISE NOTICE '🏗️  Buildings table schema:';
    RAISE NOTICE '   - bbl (unique identifier)';
    RAISE NOTICE '   - address, borough, lat/lng';
    RAISE NOTICE '   - num_floors (for barometer validation)';
    RAISE NOTICE '   - year_built, building_class, land_use';
    RAISE NOTICE '   - is_landmark, landmark_name, architect, style';
    RAISE NOTICE '   - landmark_score, final_score';
    RAISE NOTICE '   - scan_enabled, has_reference_images';
END $$;
