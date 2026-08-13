-- APPLIED 2026-08-12 to BUILDINGS (cglsuoymdcchrxyzofjb).
--
-- _get_block_context in the lore chain took 11.2 SECONDS, which is most of why a
-- cold narrative took ~20s. The table LOOKED indexed -- a btree on `bin` and two
-- GIST indexes on `centroid` already existed -- but neither matched the
-- expression the query actually filters on:
--
--   * `WHERE replace(bin,'.0','') = :bin` wraps the column in a function, so the
--     btree on `bin` cannot serve it. Seq scan, 726,349 rows removed, 5.4s, to
--     find ONE row.
--   * `ST_DWithin(f.centroid::geography, ...)` casts to geography while the GIST
--     indexes are on the geometry column. Seq scan of 1,083,141 rows to keep 90.
--
-- An index on a column does not help a query that filters on a function OF that
-- column. Index the expression.
--
-- Measured: 11,169ms -> 118ms. Buffers 78,949 -> 160.
--
-- The speedup was the smaller half of the win. A 3s timeout had been wrapped
-- around these lookups to stop them stalling generation, which meant an 11s
-- block-context query was ALWAYS discarded -- so every narrative in that window
-- silently lost the block-comparison material ("sixty years older than anything
-- on its block") that no model and no web search can produce. The index turned
-- the feature back on. A timeout that always fires is a feature that no longer
-- exists; the timeout stays as a ceiling, not as the normal path.
--
-- The `.0` float-cast suffix on BIN is pervasive in this codebase, so any other
-- table joined on replace(bin,'.0','') wants the same treatment.

CREATE INDEX IF NOT EXISTS idx_footprints_bin_normalized
  ON building_footprints (replace(bin, '.0', ''));

CREATE INDEX IF NOT EXISTS idx_footprints_centroid_geog
  ON building_footprints USING gist ((centroid::geography));

ANALYZE building_footprints;

-- Verify: both should report an Index Scan, total well under 200ms.
--
-- EXPLAIN (ANALYZE, BUFFERS)
-- WITH me AS (
--   SELECT bin, centroid, construction_year, height_roof
--   FROM building_footprints
--   WHERE replace(bin,'.0','') = '1080454' AND centroid IS NOT NULL
--   LIMIT 1
-- )
-- SELECT count(*) FROM building_footprints f, me
-- WHERE f.bin <> me.bin
--   AND ST_DWithin(f.centroid::geography, me.centroid::geography, 100);
