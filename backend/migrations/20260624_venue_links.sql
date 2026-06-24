-- Free FSQ Open Source Places link fields for venues.
--
-- The FSQ OSP parquet carries per-venue `instagram`, `website`, `tel` (and
-- `facebook_id`) — free, Apache-2.0, storable. They were dropped at ingest.
-- Surfacing them powers the VenueDetailSheet link-outs (Instagram / Website /
-- Call) WITHOUT any paid photos API. `instagram` is stored as a bare handle.
--
-- Run:  psql "$SEARCH_DB_URL" -f migrations/20260624_venue_links.sql

ALTER TABLE venues
    ADD COLUMN IF NOT EXISTS instagram TEXT,
    ADD COLUMN IF NOT EXISTS website   TEXT,
    ADD COLUMN IF NOT EXISTS tel       TEXT;
