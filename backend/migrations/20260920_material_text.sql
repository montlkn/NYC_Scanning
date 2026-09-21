-- material was added by 20260710_index_enrich.sql but never backfilled for the
-- curated corpus: 35,142 of 35,334 rows were NULL, so the `material` facet
-- filter matched almost nothing and no material query had a column to hit.
--
-- material_text is the retrieval spelling of the same value. The source says
-- "Terra Cotta" (two words, 1,502 rows across 'Brick and Terra Cotta' and
-- 'Limestone and Terra Cotta') while people type "terracotta"; neither trigram
-- nor a 384-dim embedding bridges that on its own.
--
-- Populated by scripts/backfill_index_material.py.
-- Run: psql "$SEARCH_DB_URL" -f migrations/20260920_material_text.sql

ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS material_text text;

CREATE INDEX IF NOT EXISTS idx_bsi_material_text_trgm
    ON building_search_index USING gin (lower(material_text) gin_trgm_ops);
