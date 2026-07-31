-- Preserve ALL Foursquare category labels per venue, not just one.
--
-- seed_venues.category_leaf() kept a single label chosen by `max()` on
-- '>'-count, which returns the FIRST element on a depth tie -- i.e. arbitrary
-- parquet order. A place labelled both 'Arts and Entertainment > Art Gallery'
-- and 'Dining and Drinking > Bar' got whichever came first and the
-- corroborating label was discarded, so "Patricia Shea Fine Art" was stored
-- as category='Bar' and ranked #1 for "art deco bars near me".
--
-- Keeping every label lets poi_category_adjustment detect the CONFLICT (a
-- venue claiming two unrelated FSQ domains) instead of trusting one coin flip.
--
-- Run against SEARCH_DB_URL (the Railway pgvector DB that holds `venues` and
-- `building_search_index`), then re-run scripts/seed_venues.py to populate.

ALTER TABLE venues ADD COLUMN IF NOT EXISTS category_labels  TEXT[];
ALTER TABLE venues ADD COLUMN IF NOT EXISTS category_domains TEXT[];

-- Backfill the existing single category so rows are never NULL-armed between
-- this migration and the next full re-ingest. Only the leaf is recoverable
-- from what we stored; the discarded siblings genuinely require the re-ingest.
UPDATE venues
   SET category_labels = ARRAY[category]
 WHERE category_labels IS NULL
   AND category IS NOT NULL
   AND category <> '';

-- Venues whose labels span more than one top-level FSQ domain are the
-- ambiguous ones. Indexed so the ranker's conflict check stays cheap.
CREATE INDEX IF NOT EXISTS venues_category_domains_idx
    ON venues USING GIN (category_domains);
