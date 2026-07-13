-- Fame signal for search ranking. is_landmark turned out to be useless as a
-- fame proxy: it's the LPC designation flag and the index is BUILT from
-- designated buildings, so 99.4% of rows are TRUE. `fame` is instead derived
-- from buildings_full_merge_scanning.final_score (Wikipedia/search-interest
-- composite), normalized 0–1 against the corpus max at backfill time — a
-- continuous, data-derived score with a real long tail (median ≈ 0.08,
-- Empire State ≈ 0.85). Populated by scripts/backfill_fame.py and maintained
-- by scripts/embed_buildings.py on ingest.

ALTER TABLE building_search_index ADD COLUMN IF NOT EXISTS fame real;

CREATE INDEX IF NOT EXISTS idx_bsi_fame
    ON building_search_index (fame DESC NULLS LAST);
