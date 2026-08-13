-- APPLIED 2026-08-12 to BUILDINGS (cglsuoymdcchrxyzofjb).
--
-- Kill the '.0' suffix at the SOURCE.
--
-- 35,189 of 35,380 bins and 35,220 bbls carried a float-cast suffix from a
-- pandas ingest: '1015862.0' rather than '1015862'. Every consumer had to
-- remember to normalise, and any that forgot silently missed 99.5% of the
-- table -- not an error, just no rows.
--
-- This had been patched downstream at least four separate times:
--   * expression index on building_footprints (replace(bin,'.0',''))
--   * expression index on landmark_chunks (same)
--   * enrich_building_facts normalising both sides in its WHERE
--   * the scan-history enrichment lookup
-- Each fix was correct and none of them addressed the cause. Notably
-- `building_footprints` (1.08M rows) was ALREADY clean, so the convention was
-- inconsistent across tables, which is why it kept resurfacing in new places.
--
-- Verified after: `WHERE bin = '1015862'` returns the Empire State Building.
-- That exact-match form is what the client sends, and it had been returning
-- nothing.
--
-- The CHECK constraints are the "for good" part. A convention cannot be
-- enforced; a constraint can. The next ingest that float-casts will fail loudly
-- at write time instead of silently poisoning lookups for months.
--
-- Run with a generous statement_timeout: the table is wide and carries 18
-- indexes, so the two UPDATEs took 79s and 226s. The Supabase MCP times out
-- well before that -- use psql against DATABASE_URL.

SET statement_timeout = '900s';

UPDATE buildings_full_merge_scanning
SET bin = regexp_replace(bin, '\.0+$', '')
WHERE bin ~ '\.0+$';                                   -- 29,189 rows

UPDATE buildings_full_merge_scanning
SET bbl = regexp_replace(bbl, '\.0+$', '')
WHERE bbl ~ '\.0+$';                                   -- 35,220 rows

ALTER TABLE buildings_full_merge_scanning
  ADD CONSTRAINT bin_no_float_suffix CHECK (bin IS NULL OR bin !~ '\.'),
  ADD CONSTRAINT bbl_no_float_suffix CHECK (bbl IS NULL OR bbl !~ '\.');

-- MANDATORY. Four matviews read this table and would otherwise keep serving the
-- old '.0' values -- the map and Kit picks read through these, so skipping this
-- leaves the client matching against stale keys.
REFRESH MATERIALIZED VIEW buildings_geo;
REFRESH MATERIALIZED VIEW kit_canon_candidates;
REFRESH MATERIALIZED VIEW map_prefill_rows;
REFRESH MATERIALIZED VIEW similar_candidates;

-- Verify: all three must be 0.
--   SELECT count(*) FILTER (WHERE bin LIKE '%.%'),
--          count(*) FILTER (WHERE bbl LIKE '%.%') FROM buildings_full_merge_scanning;
--   SELECT count(*) FILTER (WHERE bin LIKE '%.%') FROM map_prefill_rows;
--
-- NOT REMOVED: the replace(bin,'.0','') calls scattered through the app and the
-- expression indexes that serve them. They are now no-ops on clean data and
-- still correct, and ripping them out while the client is mid-release would be
-- a second migration for no behavioural gain. Delete them opportunistically.

-- ---------------------------------------------------------------------------
-- FOLLOW-UP, same day: year_built carried the identical float cast (35,229 of
-- 35,317 stored as '1874.0'). Spotted in a field log where KitCanonService
-- returned "year_built":"1874.0" AFTER bin/bbl were already clean -- the ingest
-- float-cast every numeric-looking column, not just the keys.
--
-- Lower severity than bin: year_built is not a join key, so it caused no silent
-- misses. It DID reach the lore prompt verbatim as "Year built: 1874.0", which
-- the model happened to clean up. That was luck, not design.
--
-- Value space checked first: only 'N.0' (35,229) and plain integers (88), no
-- 'c. 1900' style entries, so the CHECK is safe here too.

UPDATE buildings_full_merge_scanning
SET year_built = regexp_replace(year_built, '\.0+$', '')
WHERE year_built ~ '\.0+$';

ALTER TABLE buildings_full_merge_scanning
  ADD CONSTRAINT year_built_no_float_suffix
  CHECK (year_built IS NULL OR year_built !~ '\.');

-- Matviews refreshed again after this column changed too.
