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

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_venues_lex_text_trgm
    ON venues USING gin (lex_text gin_trgm_ops);
