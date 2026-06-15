-- Phase 2: server-side semantic search index — DEDICATED pgvector Postgres.
--
-- Runs on a SEPARATE Railway service (image: pgvector/pgvector:pg16), pointed at
-- by $SEARCH_DB_URL. Kept apart from the PostGIS footprints DB so that prod
-- service is never touched. No PostGIS needed here: geo filtering uses plain
-- lat/lng + Haversine in the query, and provenance (year_built etc.) is
-- denormalized into this table at ingest from the footprints/Supabase data.
--
-- Mirrors the curated buildings (Supabase `buildings_full_merge_scanning`,
-- ~36K rows) by BIN; populated by backend/scripts/embed_buildings.py.
--
-- Run:  psql "$SEARCH_DB_URL" -f migrations/20260615_building_search_index.sql

CREATE EXTENSION IF NOT EXISTS vector;

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
    updated_at   TIMESTAMPTZ DEFAULT now()
);

-- HNSW cosine index (pgvector >= 0.5). If unavailable, swap for:
--   CREATE INDEX ... USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_bsi_embedding_hnsw
    ON building_search_index USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_bsi_year ON building_search_index (year_built);
CREATE INDEX IF NOT EXISTS idx_bsi_lat  ON building_search_index (lat);
CREATE INDEX IF NOT EXISTS idx_bsi_lng  ON building_search_index (lng);
