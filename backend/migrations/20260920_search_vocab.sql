-- Derived query vocabulary, measured from the corpus instead of hand-listed.
--
-- WHY: services/unified_search.py's intent cascade gated on _STYLE_VOCAB (28
-- hardcoded words) against 761 distinct style_primary values, and _POI_NOUNS
-- (no "church", "synagogue", "library", "firehouse") against 442 distinct
-- building_type values.  A miss is expensive: it swings _INTENT_WEIGHTS by
-- 2.5x on the buildings/venues split, which is why "cast iron soho" got `name`
-- intent and came back as 2016-2018 glass towers.
--
-- Populated by scripts/build_search_vocab.py.  Membership is decided by a
-- measured discriminative ratio, not a curated stoplist: a token counts when
-- it appears inside the source column far more often than it appears anywhere
-- else in the corpus text.  That rejects "the"/"new"/"classic"/"free" on
-- evidence and keeps "italianate"/"neo-grec"/"beaux-arts" without anyone
-- typing them out.
--
-- Run: psql "$SEARCH_DB_URL" -f migrations/20260920_search_vocab.sql

CREATE TABLE IF NOT EXISTS search_vocab (
    kind        TEXT NOT NULL,          -- 'style' | 'poi' | 'material'
    term        TEXT NOT NULL,
    df_source   INTEGER NOT NULL,       -- rows whose source column holds it
    df_text     INTEGER NOT NULL,       -- rows whose embedded text holds it
    ratio       REAL   NOT NULL,        -- df_source / df_text
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (kind, term)
);
