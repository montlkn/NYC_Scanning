-- Names and aliases only, for the typo-tolerant retrieval leg.
--
-- fuzzy_pool ran similarity(query, text) -- a WHOLE-STRING trigram comparison
-- against the ~200-char embedded document, which is structurally near zero no
-- matter how good the match. Measured on the live index:
--
--   similarity('chrystler building', text)      = 0.119   (< 0.2 floor: dropped)
--   similarity('chrystler building', name_norm) = 0.640
--
-- So the fuzzy leg was dead weight and typo tolerance was a coin flip --
-- "woolwoth" found the Woolworth Building, "chrystler" never surfaced the
-- Chrysler at all.
--
-- Populated by scripts/backfill_index_name_norm.py.
-- Run: psql "$SEARCH_DB_URL" -f migrations/20260920_name_norm.sql

ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS name_norm text;

CREATE INDEX IF NOT EXISTS idx_bsi_name_norm_trgm
    ON building_search_index USING gin (lower(name_norm) gin_trgm_ops);
