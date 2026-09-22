-- Venue enrichment: where a venue is, and whether it is a place at all.
--
-- Measured 2026-09-22 against SEARCH_DB (292k venues):
--   * "seagram bar" returned Korean karaoke bars in Fort Lee and Palisades
--     Park. The ingest bbox is a rectangle, and a rectangle around NYC
--     contains half of Bergen and Hudson counties. 8,009 rows fall outside
--     even the rectangle; more sit inside it on the wrong side of the Hudson.
--   * 14,105 rows are FSQ "Structure": offices, car services, and apartment
--     buildings named by their address ("1204 Broadway", "903 Park"). They
--     are why POI results "come up as numbers". The building itself is
--     already in building_search_index.
--   * Only 10,222 venue texts said which neighborhood they are in, so
--     "modernist bars in midtown" had nothing to match "midtown" against.
--
-- Nothing is deleted. `searchable` hides a row from search and is rebuilt by
-- scripts/enrich_venues.py from the rule written there.
--
-- Every column is nullable with no default, so ADD COLUMN is metadata-only.
-- lock_timeout because it still needs ACCESS EXCLUSIVE for an instant, and
-- queueing behind a long read would block every search behind IT (this took
-- search down for ~8 min on 2026-09-21).

SET lock_timeout = '3s';

ALTER TABLE venues
    ADD COLUMN IF NOT EXISTS in_nyc       boolean,
    ADD COLUMN IF NOT EXISTS borough      text,
    ADD COLUMN IF NOT EXISTS neighborhood text,
    ADD COLUMN IF NOT EXISTS searchable   boolean,
    ADD COLUMN IF NOT EXISTS lex_text     text;

-- Run AFTER scripts/enrich_venues.py has filled lex_text (outside a
-- transaction -- CONCURRENTLY cannot run inside one):
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_venues_lex_text_trgm
--       ON venues USING gin (lex_text gin_trgm_ops);
--
-- Filtered HNSW scans. Without this an ANN query returns ef_search (40)
-- candidates and THEN applies the WHERE clause, so a radius filter or the
-- `searchable` filter quietly shrinks the vector pool to whatever survived
-- out of 40. pgvector >= 0.8 keeps scanning until the LIMIT is met. Every
-- leg re-scores its pool, so relaxed ordering costs nothing:
--
--   ALTER DATABASE railway SET hnsw.iterative_scan = 'relaxed_order';

-- PLUTO building class of the venue's lot ("church", "theater", "factory"),
-- written by scripts/enrich_venues.py. Lets "bar in a former church" match.
ALTER TABLE venues ADD COLUMN IF NOT EXISTS host_class text;
