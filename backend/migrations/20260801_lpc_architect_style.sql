-- Structured LPC attribution on building_search_index.
--
-- Source: NYC Open Data `gpmc-yuvp` (Individual Landmark and Historic District
-- Building Database) -- the same LPC dataset this index was originally built
-- from, but three of its fields were never carried across.
--
--   architect        27,638 buildings / 4,689 distinct names. The index had NO
--                    architect column, so classify_intent's `architect` branch
--                    ("buildings designed by Cass Gilbert") and
--                    infer_matched_field's architect slot had nothing to read;
--                    architect queries fell back to fuzzy matching on `text`.
--
--   style_primary /  LPC records a PRIMARY and a SECONDARY style. Collapsed
--   style_secondary  into one string, hedged values like "Simplified Colonial
--                    Revival or Art Deco" (598 rows, LPC's own wording) made a
--                    block of colonial-revival houses rank as prime Art Deco,
--                    because trigram similarity matches the best substring.
--                    Structured columns let the ranker weight a confident
--                    primary above a hedged alternative from DATA rather than
--                    by parsing prose.
--
--   hist_dist        161 districts. A real facet ("Fort Greene Historic
--                    District") the app can filter and search on.
--
-- Run against SEARCH_DB_URL, then scripts/backfill_lpc_attribution.py.

ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS architect       TEXT;
ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS style_primary   TEXT;
ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS style_secondary TEXT;
ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS hist_dist       TEXT;

-- Trigram index: architect search is name-fuzzy by nature ("cass gilbert",
-- "Gilbert, Cass", "C. Gilbert"), so it goes through word_similarity like the
-- other lexical paths rather than exact match.
CREATE INDEX IF NOT EXISTS bsi_architect_trgm_idx
    ON building_search_index USING GIN (lower(architect) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS bsi_hist_dist_idx
    ON building_search_index (hist_dist);
