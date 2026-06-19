-- Tier 3 hybrid ranking: add a lexical (trigram) signal to building search.
--
-- Pure pgvector cosine is strong on style/material CONCEPTS but weak on PROPER
-- NOUNS — "chrysler" returned RCA Building, "neil denari" returned unrelated
-- brownstones, because bge-small weights a name equally with the surrounding
-- spec-sheet tokens. The `text` column already holds the embedded string
-- (name + architect + style + address + storytelling), so a trigram match on
-- `text` recovers the proper noun. routers/search.py fuses it with cosine:
--   score = 0.7 * cosine + 0.3 * word_similarity(lower(text), lower(:q))
--
-- pg_trgm's word_similarity() compares the query against the best-matching
-- SUBSTRING of `text`, so a short name query ("chrysler") isn't penalised by the
-- long descriptive text around it. The GIN trigram index keeps it fast.
--
-- The router does a two-stage query (ANN candidate pool, then re-rank by fused
-- score), so the existing HNSW embedding index still drives recall; this index
-- accelerates the word_similarity() re-rank.
--
-- ⚠️ Without pg_trgm the new SELECT in routers/search.py errors and /api/search
--    returns [] (client falls back). Run this BEFORE deploying that change.
--
-- Run:  psql "$SEARCH_DB_URL" -f migrations/20260619_hybrid_trigram.sql

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GIN trigram index over the lowered embedded text — backs word_similarity().
CREATE INDEX IF NOT EXISTS idx_bsi_text_trgm
    ON building_search_index USING gin (lower(text) gin_trgm_ops);
