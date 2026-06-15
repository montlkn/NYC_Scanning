-- Phase 2: server-side semantic search index (Railway).
--
-- Mirrors the curated buildings (Supabase `buildings_full_merge_scanning`,
-- ~36K rows) by BIN, holding the bge-small text embedding + denormalized
-- filter columns. Populated by backend/scripts/embed_buildings.py. Supabase
-- stays the source-of-truth; only the index lives here, beside PLUTO /
-- building_footprints, so semantic search AND the venue→building provenance
-- join stay local (no cross-DB round trips).
--
-- Run:  psql "$FOOTPRINTS_DB_URL" -f migrations/20260615_building_search_index.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;  -- already present (building_footprints uses geography)

CREATE TABLE IF NOT EXISTS building_search_index (
    bin          TEXT PRIMARY KEY,
    bbl          TEXT,
    text         TEXT NOT NULL,            -- the exact text that was embedded (inspect/debug)
    snippet      TEXT,                     -- short display string for results
    embedding    vector(384) NOT NULL,     -- BAAI/bge-small-en-v1.5, L2-normalized
    year_built   INTEGER,                  -- era filter
    bldgclass    TEXT,
    is_landmark  BOOLEAN DEFAULT FALSE,
    lat          DOUBLE PRECISION,
    lng          DOUBLE PRECISION,
    -- Generated geography for geo filter/sort. ST_MakePoint/ST_SetSRID + the
    -- geography cast are IMMUTABLE, so a STORED generated column is valid. If a
    -- given PostGIS build rejects it, drop the GENERATED clause and backfill via
    -- a BEFORE INSERT/UPDATE trigger instead.
    geog         geography(Point, 4326) GENERATED ALWAYS AS (
                     CASE WHEN lat IS NOT NULL AND lng IS NOT NULL
                          THEN ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography
                     END
                 ) STORED,
    updated_at   TIMESTAMPTZ DEFAULT now()
);

-- HNSW cosine index (pgvector >= 0.5). If unavailable, replace with:
--   CREATE INDEX ... USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_bsi_embedding_hnsw
    ON building_search_index USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_bsi_geog ON building_search_index USING gist (geog);
CREATE INDEX IF NOT EXISTS idx_bsi_year ON building_search_index (year_built);
