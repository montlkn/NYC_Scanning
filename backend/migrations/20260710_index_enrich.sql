-- Unified search index enrichment: filter columns (style/borough/material),
-- personalization vector, and photo thumbnails across the three search-DB
-- corpora (building_search_index, venues, layer_search_index).
--
-- Why: /api/search/unified (routers/search.py, services/unified_search.py,
-- see 20260710_unified_search.sql) accepts style_family/borough/material/
-- lore_status/photo_url/personalization params but has nowhere to source them
-- from — building_search_index has no normalized style/borough/material
-- columns (style is best-effort parsed out of `snippet` text), venues/layers
-- have no photo_url, and layer_search_index has no dedicated lore_status
-- column (only overloaded via `category`). This migration adds the columns;
-- backend/scripts/{embed_buildings,embed_layers,seed_venues}.py backfill them
-- on next run (see those scripts' updated upserts).
--
-- profile vector(9): chosen over float8[] because pgvector's `<#>` (negative
-- inner product) operator gives a dot-product-based personalization score for
-- free in SQL (and in Python via the same driver conventions already used for
-- the 384-dim embedding column) without hand-rolling array math — and the
-- table already depends on the `vector` extension for `embedding`, so this
-- adds no new extension dependency. 9 = the AestheticProfile archetype count
-- (Classicist/Romantic/Stylist/Modernist/Industrialist/Visionary/
-- PopCulturalist/Vernacularist/Austerist per Jink_Swift's AestheticProfile).
--
-- Degrade-gracefully note: all columns are nullable and additive
-- (ADD COLUMN IF NOT EXISTS), so this migration is idempotent and safe to run
-- any time — old rows simply read NULL for the new columns until the next
-- ingest run backfills them, and routers/search.py already wraps every new-
-- column read in the same try/except-on-missing-column pattern used for the
-- trigram legs (see 20260710_unified_search.sql's note), so the service does
-- not need this migration applied before it can deploy.
--
-- Run:  psql "$SEARCH_DB_URL" -f migrations/20260710_index_enrich.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- building_search_index -----------------------------------------------------
ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS style_family text;
ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS borough      text;
ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS material     text;
ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS profile      vector(9);
ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS photo_url    text;

CREATE INDEX IF NOT EXISTS idx_bsi_style_family ON building_search_index (style_family);
CREATE INDEX IF NOT EXISTS idx_bsi_borough      ON building_search_index (borough);
CREATE INDEX IF NOT EXISTS idx_bsi_material     ON building_search_index (material);

-- venues ----------------------------------------------------------------
ALTER TABLE venues ADD COLUMN IF NOT EXISTS photo_url text;

-- layer_search_index ---------------------------------------------------
ALTER TABLE layer_search_index ADD COLUMN IF NOT EXISTS lore_status text;
ALTER TABLE layer_search_index ADD COLUMN IF NOT EXISTS photo_url   text;

CREATE INDEX IF NOT EXISTS idx_layer_lore_status ON layer_search_index (lore_status);
