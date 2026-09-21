-- Per-building LPC designation-report prose as a 4th retrieval leg.
--
-- WHY: building_search_index.text is a metadata template ("Lever House, an
-- international style office building in Manhattan. designed by ...").  Only 51
-- of 35,382 source rows carry any free prose, so the embedding has nothing
-- descriptive to match on and queries like "gargoyles", "stained glass" or
-- "terracotta" return pure noise.  The LPC designation reports DO carry that
-- detail ("half-timbering with carved gargoyles"), 121,466 chunks of it on the
-- footprints DB, and search has never touched them.
--
-- WHY ONLY specificity='building': the NULL-specificity chunks are district
-- boilerplate — 52,182 chunks sharing just 977 distinct texts, one paragraph
-- spread over as many as 1,952 BINs.  Embedding those makes every building in
-- a historic district score identically and actively destroys ranking.  The
-- building-specific set is 69,284 chunks over 55,087 distinct texts.
--
-- Run: psql "$SEARCH_DB_URL" -f migrations/20260920_building_lore_index.sql

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS building_lore_index (
    id           BIGSERIAL PRIMARY KEY,
    bin          TEXT NOT NULL,
    text         TEXT NOT NULL,
    text_hash    TEXT NOT NULL,           -- md5, so one forward pass per distinct text
    embedding    vector(384) NOT NULL,    -- BAAI/bge-small-en-v1.5, L2-normalized
    source_file  TEXT,
    page_number  INTEGER,
    updated_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (bin, text_hash)
);

-- The leg rolls up MAX(similarity) per BIN, so the HNSW scan must be able to
-- over-fetch chunks and still land enough distinct BINs.
CREATE INDEX IF NOT EXISTS idx_bli_embedding_hnsw
    ON building_lore_index USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_bli_bin  ON building_lore_index (bin);
CREATE INDEX IF NOT EXISTS idx_bli_trgm ON building_lore_index USING gin (lower(text) gin_trgm_ops);
