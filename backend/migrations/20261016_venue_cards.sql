-- Search DB (Railway pgvector). Run as the owner login (SEARCH_DB_WRITE_URL).
--
-- Venue cards: a short description of a venue written by Kit (gpt-5.6-luna)
-- from web search results only, then fact-checked against those results
-- (services/venue_cards.py). They used to live in cards/venue_cards.jsonl,
-- shipped with the API image, so a card written by a job after deploy could
-- never reach the live server. The API now reloads this table every 30
-- minutes (services/vibe_judge.run_forever); the JSONL is still read as a
-- fallback until it has been imported (build_venue_cards --import-jsonl).

CREATE TABLE IF NOT EXISTS venue_cards (
    fsq_id      text PRIMARY KEY,          -- venues.fsq_id (FSQ, Overture or apple:)
    name        text,
    category    text,
    match       boolean NOT NULL DEFAULT false,  -- results are about THIS venue
    found       boolean NOT NULL DEFAULT false,  -- the web had any results at all
    closed      boolean NOT NULL DEFAULT false,
    kind        text,                      -- what it really is: "chess shop", "dive bar"
    card        text,                      -- 2-3 sentences, empty when unusable
    sources     text[],
    judge       text,                      -- NULL = passed or not judged; else why rejected
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS venue_cards_updated ON venue_cards (updated_at);

-- The API reads as jink_search_app (read-only).
GRANT SELECT ON venue_cards TO jink_search_app;
