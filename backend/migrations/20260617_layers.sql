-- Layer search index — folds the OTHER map layers (lore events, plaques,
-- community contributions) into the SAME dedicated pgvector DB as
-- building_search_index / venues ($SEARCH_DB_URL).
--
-- One query → all data. Each row is embedded with the SAME bge-small model as
-- buildings/venues so "1977 blackout" or "Poe plaque" rank semantically. The
-- iOS app uses these hits to LIGHT UP + FILTER the matching map layer (no new
-- card types), so we only need id/coords/title/snippet back per hit.
--
-- Source rows live on the MAIN Supabase (DATABASE_URL) — the same DB
-- embed_buildings.py reads — so no new DB access is required.
--
-- Run:  psql "$SEARCH_DB_URL" -f migrations/20260617_layers.sql

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS layer_search_index (
    id          TEXT PRIMARY KEY,         -- "lore:<uuid>" | "plaque:<uuid>" | "contribution:<uuid>"
    layer       TEXT NOT NULL,            -- 'lore' | 'plaque' | 'contribution'
    title       TEXT,                     -- display headline
    snippet     TEXT,                     -- short display string
    text        TEXT NOT NULL,            -- exact embedded text (inspect/debug)
    embedding   vector(384) NOT NULL,     -- BAAI/bge-small-en-v1.5, L2-normalized
    lat         DOUBLE PRECISION,
    lng         DOUBLE PRECISION,
    year        INTEGER,
    category    TEXT,                     -- lore category / plaque series / null
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_layer_embedding_hnsw
    ON layer_search_index USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_layer_layer ON layer_search_index (layer);
CREATE INDEX IF NOT EXISTS idx_layer_lat   ON layer_search_index (lat);
CREATE INDEX IF NOT EXISTS idx_layer_lng   ON layer_search_index (lng);
