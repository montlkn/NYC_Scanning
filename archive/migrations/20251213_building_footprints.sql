-- Migration: Create building_footprints table for NYC building geometry
-- This table stores polygon footprints for ALL 1.08M NYC buildings
-- Used for bulletproof GPS + compass building identification

-- Enable PostGIS if not already enabled
CREATE EXTENSION IF NOT EXISTS postgis;

-- Drop table if recreating (comment out in production)
-- DROP TABLE IF EXISTS building_footprints;

-- Create building footprints table
CREATE TABLE IF NOT EXISTS building_footprints (
    -- Primary key: Building Identification Number
    bin TEXT PRIMARY KEY,

    -- Secondary identifier for lot-level joins
    bbl TEXT,

    -- Building name (often NULL for residential)
    name TEXT,

    -- Geometry columns (SRID 4326 = WGS84 lat/lng)
    footprint GEOMETRY(MULTIPOLYGON, 4326),
    centroid GEOMETRY(POINT, 4326),

    -- Physical attributes
    height_roof FLOAT,           -- Height in feet from ground to roof
    ground_elevation FLOAT,      -- Ground elevation in feet
    shape_area FLOAT,            -- Footprint area in square feet
    construction_year INT,       -- Year built

    -- Metadata
    feature_code INT,            -- NYC building type code
    geometry_source TEXT,        -- Source of geometry data
    last_edited_date TIMESTAMPTZ,

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- CRITICAL: Spatial index on footprint polygon
-- This is what makes ST_Intersects fast (O(log n) instead of O(n))
CREATE INDEX IF NOT EXISTS idx_footprints_footprint_gist
    ON building_footprints USING GIST (footprint);

-- Spatial index on centroid for distance calculations
CREATE INDEX IF NOT EXISTS idx_footprints_centroid_gist
    ON building_footprints USING GIST (centroid);

-- Index for joining with other tables by BBL
CREATE INDEX IF NOT EXISTS idx_footprints_bbl
    ON building_footprints (bbl);

-- Index for height-based queries (taller buildings first)
CREATE INDEX IF NOT EXISTS idx_footprints_height
    ON building_footprints (height_roof DESC NULLS LAST);

-- Index for construction year (for filtering by era)
CREATE INDEX IF NOT EXISTS idx_footprints_year
    ON building_footprints (construction_year);

-- Create a function to generate view cone polygon
-- This is used in the main query to find buildings in user's view
CREATE OR REPLACE FUNCTION generate_view_cone(
    user_lat DOUBLE PRECISION,
    user_lng DOUBLE PRECISION,
    bearing DOUBLE PRECISION,       -- Compass bearing 0-360
    distance_m DOUBLE PRECISION,    -- Max distance in meters
    cone_angle DOUBLE PRECISION     -- Total cone angle in degrees
)
RETURNS GEOMETRY AS $$
DECLARE
    R CONSTANT DOUBLE PRECISION := 6371000;  -- Earth radius in meters
    user_point GEOMETRY;
    left_bearing DOUBLE PRECISION;
    right_bearing DOUBLE PRECISION;
    points TEXT[];
    i INT;
    angle DOUBLE PRECISION;
    dest_lat DOUBLE PRECISION;
    dest_lng DOUBLE PRECISION;
BEGIN
    user_point := ST_SetSRID(ST_MakePoint(user_lng, user_lat), 4326);

    -- Calculate left and right edges of cone
    left_bearing := bearing - (cone_angle / 2);
    right_bearing := bearing + (cone_angle / 2);

    -- Start with user position
    points := ARRAY[user_lng || ' ' || user_lat];

    -- Generate arc points (12 points for smooth cone)
    FOR i IN 0..12 LOOP
        angle := left_bearing + (cone_angle * i / 12);

        -- Calculate destination point using haversine
        dest_lat := DEGREES(ASIN(
            SIN(RADIANS(user_lat)) * COS(distance_m / R) +
            COS(RADIANS(user_lat)) * SIN(distance_m / R) * COS(RADIANS(angle))
        ));

        dest_lng := user_lng + DEGREES(ATAN2(
            SIN(RADIANS(angle)) * SIN(distance_m / R) * COS(RADIANS(user_lat)),
            COS(distance_m / R) - SIN(RADIANS(user_lat)) * SIN(RADIANS(dest_lat))
        ));

        points := array_append(points, dest_lng || ' ' || dest_lat);
    END LOOP;

    -- Close polygon back to start
    points := array_append(points, user_lng || ' ' || user_lat);

    RETURN ST_SetSRID(
        ST_GeomFromText('POLYGON((' || array_to_string(points, ', ') || '))'),
        4326
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Create the main query function for building identification
CREATE OR REPLACE FUNCTION find_buildings_in_cone(
    user_lat DOUBLE PRECISION,
    user_lng DOUBLE PRECISION,
    bearing DOUBLE PRECISION,
    max_distance_m DOUBLE PRECISION DEFAULT 100,
    cone_angle DOUBLE PRECISION DEFAULT 60,
    max_results INT DEFAULT 20
)
RETURNS TABLE (
    bin TEXT,
    bbl TEXT,
    name TEXT,
    distance_meters DOUBLE PRECISION,
    bearing_to_building DOUBLE PRECISION,
    bearing_difference DOUBLE PRECISION,
    visible_area DOUBLE PRECISION,
    shape_area DOUBLE PRECISION,
    height_roof DOUBLE PRECISION,
    visibility_score DOUBLE PRECISION
) AS $$
DECLARE
    cone GEOMETRY;
    user_point GEOMETRY;
BEGIN
    -- Generate view cone
    cone := generate_view_cone(user_lat, user_lng, bearing, max_distance_m, cone_angle);
    user_point := ST_SetSRID(ST_MakePoint(user_lng, user_lat), 4326);

    RETURN QUERY
    WITH candidates AS (
        SELECT
            bf.bin,
            bf.bbl,
            bf.name,
            bf.centroid,
            bf.footprint,
            bf.shape_area AS total_area,
            bf.height_roof,
            -- Distance from user to building centroid
            ST_Distance(bf.centroid::geography, user_point::geography) AS dist_m,
            -- Area of footprint visible in cone
            ST_Area(ST_Intersection(bf.footprint, cone)::geography) AS visible_sqm
        FROM building_footprints bf
        WHERE ST_Intersects(bf.footprint, cone)
    )
    SELECT
        c.bin,
        c.bbl,
        c.name,
        c.dist_m AS distance_meters,
        -- Calculate bearing from user to building
        DEGREES(ST_Azimuth(user_point, c.centroid)) AS bearing_to_building,
        -- Calculate bearing difference (0-180)
        ABS(
            MOD(
                (DEGREES(ST_Azimuth(user_point, c.centroid)) - bearing + 180 + 360)::numeric,
                360::numeric
            )::double precision - 180
        ) AS bearing_difference,
        c.visible_sqm AS visible_area,
        c.total_area AS shape_area,
        COALESCE(c.height_roof, 30) AS height_roof,
        -- Calculate visibility score (0-100)
        (
            -- Distance score (40% weight) - exponential decay
            0.40 * EXP(-c.dist_m / 30) +
            -- Bearing alignment (30% weight)
            0.30 * GREATEST(0, 1 - ABS(MOD((DEGREES(ST_Azimuth(user_point, c.centroid)) - bearing + 180 + 360)::numeric, 360::numeric)::double precision - 180) / (cone_angle / 2)) +
            -- Visible area score (20% weight)
            0.20 * LEAST(1.0, c.visible_sqm / NULLIF(c.total_area * 0.0929, 0)) +  -- Convert sqft to sqm
            -- Height score (10% weight)
            0.10 * LEAST(1.0, COALESCE(c.height_roof, 30) / 200)
        ) * 100 AS visibility_score
    FROM candidates c
    ORDER BY visibility_score DESC
    LIMIT max_results;
END;
$$ LANGUAGE plpgsql STABLE;

-- Create index to speed up the join with buildings_full_merge_scanning
-- This allows enriching footprint results with building metadata
CREATE INDEX IF NOT EXISTS idx_buildings_scanning_bin
    ON buildings_full_merge_scanning (REPLACE(bin, '.0', ''));

-- Analyze tables for query optimization
ANALYZE building_footprints;

-- Grant permissions (adjust as needed for your setup)
-- GRANT SELECT ON building_footprints TO authenticated;
-- GRANT EXECUTE ON FUNCTION find_buildings_in_cone TO authenticated;
-- GRANT EXECUTE ON FUNCTION generate_view_cone TO authenticated;

COMMENT ON TABLE building_footprints IS 'NYC building footprint polygons for GPS+compass building identification. 1.08M buildings.';
COMMENT ON FUNCTION find_buildings_in_cone IS 'Find buildings within user view cone, scored by visibility. Primary function for V2 scan system.';
COMMENT ON FUNCTION generate_view_cone IS 'Generate a polygon representing user view cone from GPS position and compass bearing.';
