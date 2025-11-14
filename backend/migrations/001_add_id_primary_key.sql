-- Migration: Add ID primary key and update constraints for BIN/BBL
-- Date: 2025-11-14
-- Purpose: Support BIN as unique identifier while handling edge cases

-- Step 1: Add auto-incrementing ID column
ALTER TABLE buildings_full_merge_scanning
ADD COLUMN id SERIAL;

-- Step 2: Drop existing primary key constraint on BBL
ALTER TABLE buildings_full_merge_scanning
DROP CONSTRAINT IF EXISTS buildings_full_merge_scanning_pkey;

-- Step 3: Make ID the new primary key
ALTER TABLE buildings_full_merge_scanning
ADD PRIMARY KEY (id);

-- Step 4: Create unique index on BIN (where not null)
-- This allows NULL BINs but enforces uniqueness for non-null values
CREATE UNIQUE INDEX idx_buildings_bin_unique
ON buildings_full_merge_scanning(bin)
WHERE bin IS NOT NULL;

-- Step 5: Create regular index on BBL (non-unique, allows duplicates)
DROP INDEX IF EXISTS idx_buildings_bbl;
CREATE INDEX idx_buildings_bbl
ON buildings_full_merge_scanning(bbl);

-- Step 6: Create index on BBL + BIN combination for queries
CREATE INDEX idx_buildings_bbl_bin
ON buildings_full_merge_scanning(bbl, bin);

-- Step 7: Add comment to explain the schema
COMMENT ON COLUMN buildings_full_merge_scanning.id IS
'Auto-incrementing primary key. Used as universal identifier for buildings.';

COMMENT ON COLUMN buildings_full_merge_scanning.bin IS
'NYC Building Identification Number. Unique per building (99.91% coverage). NULL for ~32 buildings.';

COMMENT ON COLUMN buildings_full_merge_scanning.bbl IS
'Borough-Block-Lot tax identifier. NOT unique - multiple buildings can share same BBL (e.g., WTC complex).';

-- Step 8: Create view for backward compatibility
CREATE OR REPLACE VIEW buildings_by_bbl AS
SELECT DISTINCT ON (bbl)
    id, bbl, bin, address, borough, latitude, longitude,
    landmark_name, is_landmark, walk_score, scan_enabled
FROM buildings_full_merge_scanning
WHERE bbl IS NOT NULL
ORDER BY bbl, walk_score DESC NULLS LAST, id;

COMMENT ON VIEW buildings_by_bbl IS
'Backward compatibility view: Returns one building per BBL (highest walk_score).';

-- Step 9: Create function to get building ID by BIN or BBL
CREATE OR REPLACE FUNCTION get_building_id(
    p_bin VARCHAR DEFAULT NULL,
    p_bbl VARCHAR DEFAULT NULL
) RETURNS INT AS $$
DECLARE
    v_id INT;
BEGIN
    -- Try BIN first (most reliable)
    IF p_bin IS NOT NULL THEN
        SELECT id INTO v_id
        FROM buildings_full_merge_scanning
        WHERE bin = p_bin
        LIMIT 1;

        IF v_id IS NOT NULL THEN
            RETURN v_id;
        END IF;
    END IF;

    -- Fallback to BBL (return highest scoring)
    IF p_bbl IS NOT NULL THEN
        SELECT id INTO v_id
        FROM buildings_full_merge_scanning
        WHERE bbl = p_bbl
        ORDER BY walk_score DESC NULLS LAST, id
        LIMIT 1;

        RETURN v_id;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_building_id IS
'Helper function to get building ID. Prefers BIN, falls back to BBL (returns highest scoring).';
