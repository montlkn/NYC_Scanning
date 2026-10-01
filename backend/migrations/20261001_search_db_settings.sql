-- Search DB settings that live outside any table, so a rebuild from the
-- other migrations silently loses them.
--
-- Both were only comments in 20260922_venue_enrichment.sql. When the pgvector
-- volume was lost on 2026-09-30 the tables came back from migrations/ and these
-- did not. See docs/REBUILD_SEARCH_INDEX.md.
--
--   * hnsw.iterative_scan = relaxed_order. Without it an ANN query returns
--     ef_search (40) candidates and THEN applies the WHERE clause, so a radius
--     or `searchable` filter shrinks the pool to whatever survived: a 1.5km
--     radius returned 6 of 80.
--   * Trigram GIN index on venues.lex_text, for the lexical venue leg.
--   * layer_search_index.in_nyc and venues.source, which no migration created.
--   * jink_search_app's write grants on the query log and rewrite cache.
--
-- Run as the pgvector owner, outside a transaction (CONCURRENTLY cannot run
-- inside one), one statement at a time:
--
--   psql "$SEARCH_DB_WRITE_URL" -f migrations/20261001_search_db_settings.sql
--
-- Idempotent. ALTER DATABASE applies to new sessions: redeploy the API after.

ALTER DATABASE railway SET hnsw.iterative_scan = 'relaxed_order';

SET lock_timeout = '3s';

-- Only scripts/flag_layers_in_nyc.py created this, but ingest_wikipedia_geo
-- writes it, so a fresh DB failed the Wikipedia ingest until the flag ran.
ALTER TABLE layer_search_index ADD COLUMN IF NOT EXISTS in_nyc boolean;

-- Only scripts/ingest_overture_places.py created this, but enrich_venues reads
-- it, so on a fresh DB the enrich at the end of seed_venues failed.
ALTER TABLE venues ADD COLUMN IF NOT EXISTS source text;

-- The API's role is read-only except for its query log and rewrite cache.
-- Recreated with SELECT only, every search failed to log and every vague
-- query paid the model wait, since the cache could never fill. Assumes the
-- role exists (CREATE ROLE jink_search_app LOGIN PASSWORD ...; GRANT SELECT
-- ON ALL TABLES IN SCHEMA public TO jink_search_app;).
GRANT INSERT ON search_query_log TO jink_search_app;
GRANT USAGE ON SEQUENCE search_query_log_id_seq TO jink_search_app;
GRANT INSERT, UPDATE ON search_interpretation_cache TO jink_search_app;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_venues_lex_text_trgm
    ON venues USING gin (lex_text gin_trgm_ops);
