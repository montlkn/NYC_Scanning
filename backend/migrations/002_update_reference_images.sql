-- Migration: Update reference_images table to use building ID
-- Date: 2025-11-14
-- Purpose: Support new ID-based building references

-- Step 1: Add building_id column
ALTER TABLE reference_images
ADD COLUMN building_id INT;

-- Step 2: Populate building_id from existing BBL column
-- Use the helper function to resolve BBL -> ID
UPDATE reference_images
SET building_id = (
    SELECT id
    FROM buildings_full_merge_scanning
    WHERE buildings_full_merge_scanning.bbl = reference_images."BBL"
    ORDER BY walk_score DESC NULLS LAST, id
    LIMIT 1
)
WHERE "BBL" IS NOT NULL;

-- Step 3: Add foreign key constraint
ALTER TABLE reference_images
ADD CONSTRAINT fk_reference_images_building
FOREIGN KEY (building_id)
REFERENCES buildings_full_merge_scanning(id)
ON DELETE CASCADE;

-- Step 4: Create index on building_id
CREATE INDEX idx_reference_images_building_id
ON reference_images(building_id);

-- Step 5: Add BIN column for faster lookups (denormalized)
ALTER TABLE reference_images
ADD COLUMN bin VARCHAR(7);

-- Step 6: Populate BIN from buildings table
UPDATE reference_images ri
SET bin = b.bin
FROM buildings_full_merge_scanning b
WHERE ri.building_id = b.id;

-- Step 7: Create composite index for fast queries
CREATE INDEX idx_reference_images_building_bearing
ON reference_images(building_id, compass_bearing);

CREATE INDEX idx_reference_images_bin_bearing
ON reference_images(bin, compass_bearing)
WHERE bin IS NOT NULL;

-- Step 8: Update existing BBL index
DROP INDEX IF EXISTS idx_reference_images_bbl;
CREATE INDEX idx_reference_images_bbl
ON reference_images("BBL");

-- Step 9: Add comments
COMMENT ON COLUMN reference_images.building_id IS
'Foreign key to buildings_full_merge_scanning.id - universal building identifier';

COMMENT ON COLUMN reference_images.bin IS
'Denormalized BIN for fast lookups. NULL for buildings without BIN.';

COMMENT ON COLUMN reference_images."BBL" IS
'Legacy BBL column - kept for backward compatibility';

-- Step 10: Create view for backward compatibility
CREATE OR REPLACE VIEW reference_images_by_bbl AS
SELECT
    id,
    "BBL" as bbl,
    bin,
    building_id,
    image_url,
    thumbnail_url,
    source,
    compass_bearing,
    quality_score,
    created_at
FROM reference_images
ORDER BY building_id, compass_bearing;

-- Step 11: Create helper function to get reference images
CREATE OR REPLACE FUNCTION get_reference_images(
    p_building_id INT,
    p_bearing FLOAT DEFAULT NULL,
    p_tolerance FLOAT DEFAULT 30.0
) RETURNS TABLE (
    id INT,
    image_url TEXT,
    thumbnail_url TEXT,
    compass_bearing FLOAT,
    quality_score FLOAT
) AS $$
BEGIN
    IF p_bearing IS NOT NULL THEN
        -- Return images within bearing tolerance
        RETURN QUERY
        SELECT
            ri.id,
            ri.image_url,
            ri.thumbnail_url,
            ri.compass_bearing,
            ri.quality_score
        FROM reference_images ri
        WHERE ri.building_id = p_building_id
        AND ri.compass_bearing BETWEEN p_bearing - p_tolerance AND p_bearing + p_tolerance
        ORDER BY ri.quality_score DESC
        LIMIT 5;
    ELSE
        -- Return all images for building
        RETURN QUERY
        SELECT
            ri.id,
            ri.image_url,
            ri.thumbnail_url,
            ri.compass_bearing,
            ri.quality_score
        FROM reference_images ri
        WHERE ri.building_id = p_building_id
        ORDER BY ri.compass_bearing;
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_reference_images IS
'Get reference images for a building, optionally filtered by bearing tolerance';
