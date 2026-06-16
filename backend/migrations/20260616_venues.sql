-- Phase 2 venue layer — venues table on the SAME dedicated pgvector DB as
-- building_search_index ($SEARCH_DB_URL).
--
-- Venues come from Foursquare Open Source Places (Apache-2.0, storable). Each
-- venue is geo-joined to its host building at ingest, so the building's
-- provenance (year_built, style) is DENORMALIZED onto the venue row AND baked
-- into the venue's embedding text. That join is the moat: "original midcentury
-- bar" matches because the venue's embedded text literally carries the era.
--
-- Run:  psql "$SEARCH_DB_URL" -f migrations/20260616_venues.sql

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS venues (
    fsq_id       TEXT PRIMARY KEY,         -- Foursquare OSP place id
    name         TEXT NOT NULL,
    category     TEXT,                     -- human-readable primary category
    category_id  TEXT,                     -- FSQ category id (filtering)
    text         TEXT NOT NULL,            -- exact embedded text (inspect/debug)
    snippet      TEXT,                     -- short display string
    embedding    vector(384) NOT NULL,     -- BAAI/bge-small-en-v1.5, L2-normalized
    lat          DOUBLE PRECISION,
    lng          DOUBLE PRECISION,
    -- Host-building provenance (geo-joined to building_search_index at ingest).
    -- NULL when no building matched within the join radius.
    bin          TEXT,
    bbl          TEXT,
    building_year INTEGER,                 -- host building year_built (era moat)
    building_style TEXT,
    updated_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_venues_embedding_hnsw
    ON venues USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_venues_lat  ON venues (lat);
CREATE INDEX IF NOT EXISTS idx_venues_lng  ON venues (lng);
CREATE INDEX IF NOT EXISTS idx_venues_bin  ON venues (bin);
CREATE INDEX IF NOT EXISTS idx_venues_year ON venues (building_year);
CREATE INDEX IF NOT EXISTS idx_venues_cat  ON venues (category_id);
