-- F1: Re-key reference_embeddings from buildings_full_merge_scanning.id to BIN.
--
-- Before this migration the cache could only store embeddings for BINs that
-- exist in buildings_full_merge_scanning (~10K curated buildings). After, any
-- BIN in PLUTO/building_footprints can have a cached embedding, so the cache
-- accumulates across the full 1M-building NYC inventory as users scan it.
--
-- Strategy: add `bin` column, backfill from the existing join, index. Keep
-- `building_id` column for one deploy cycle so a rollback works without data
-- loss. Drop building_id in a follow-up migration after stability is confirmed.

ALTER TABLE reference_embeddings
  ADD COLUMN IF NOT EXISTS bin TEXT;

UPDATE reference_embeddings re
   SET bin = REPLACE(b.bin::text, '.0', '')
   FROM buildings_full_merge_scanning b
  WHERE b.id = re.building_id
    AND re.bin IS NULL;

CREATE INDEX IF NOT EXISTS idx_reference_embeddings_bin
  ON reference_embeddings (bin);

-- Add columns the reference chain (F1b + F2) will populate going forward.
ALTER TABLE reference_embeddings
  ADD COLUMN IF NOT EXISTS reference_source TEXT,
  ADD COLUMN IF NOT EXISTS google_place_id  TEXT;

-- The uniqueness constraint was (building_id, angle, pitch). We replace it
-- with (bin, angle, pitch) so multiple embeddings per BIN (different angles)
-- still work. Drop the old constraint after verifying the new one holds.
-- NOTE: deferred — done in a follow-up migration once code uses bin only.
