-- Unified search (GET /api/search/unified) support:
--   1. Extend hybrid trigram fusion to venues + layer_search_index (buildings
--      already got this in 20260619_hybrid_trigram.sql).
--   2. search_interpretation_cache — caches Grok's prose-query filter-hint
--      expansion so it's never on the response critical path (see
--      routers/search.py::_get_cached_interpretation /
--      _refine_and_cache_interpretation).
--   3. search_query_log — best-effort analytics log of every /unified call.
--
-- Degrade-gracefully note: routers/search.py's venues/layers legs try the
-- hybrid (trigram) SQL first and fall back to pure-vector on ANY exception
-- (missing extension, missing index, missing table) — so this migration can
-- be run AFTER the code deploys without an outage; it just means degraded
-- (vector-only) ranking on venues/layers until it's applied. The cache/log
-- tables are wrapped the same way (best-effort, try/except) so a missing
-- table there silently no-ops rather than breaking search.
--
-- Run:  psql "$SEARCH_DB_URL" -f migrations/20260710_unified_search.sql

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Venues: trigram index over name + snippet (mirrors the buildings pattern —
-- word_similarity()/similarity() against the concatenation used in
-- routers/search.py::_leg_venues).
CREATE INDEX IF NOT EXISTS idx_venues_name_snippet_trgm
    ON venues USING gin (lower(name || ' ' || coalesce(snippet, '')) gin_trgm_ops);

-- Layers: trigram index over title + snippet.
CREATE INDEX IF NOT EXISTS idx_layer_title_snippet_trgm
    ON layer_search_index USING gin (lower(coalesce(title, '') || ' ' || coalesce(snippet, '')) gin_trgm_ops);

-- Grok prose-query interpretation cache. Keyed on the lowercased/trimmed raw
-- query string (simple exact-match cache; no normalization beyond that).
CREATE TABLE IF NOT EXISTS search_interpretation_cache (
    query          TEXT PRIMARY KEY,
    interpretation JSONB NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT now()
);

-- Best-effort query log for /api/search/unified.
CREATE TABLE IF NOT EXISTS search_query_log (
    id          BIGSERIAL PRIMARY KEY,
    query       TEXT NOT NULL,
    intent      TEXT,
    latency_ms  DOUBLE PRECISION,
    result_ids  JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_search_query_log_created_at ON search_query_log (created_at);
