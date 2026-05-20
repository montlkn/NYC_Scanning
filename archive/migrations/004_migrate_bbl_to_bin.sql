-- NYC Scan Backend - Database Migration 004
-- MIGRATE FROM BBL TO BIN AS PRIMARY IDENTIFIER
--
-- This migration converts the system from using BBL (Borough-Block-Lot) as the primary
-- building identifier to using BIN (Building Identification Number). This is necessary
-- because:
-- 1. Multiple buildings can exist on a single BBL (complex lots)
-- 2. BIN uniquely identifies individual buildings
-- 3. 99.88% of buildings have BINs, providing excellent coverage
--
-- IMPORTANT: Backup your database before running this migration!
--
-- Expected results after migration:
-- - 37,192 buildings with real BINs
-- - 17 public spaces marked as 'N/A' (parks, piers, etc.)
-- - 28 buildings still missing BINs (researched but not found, or genuinely without)
--
-- Duration: ~5-10 minutes depending on database size
-- Downtime: Minimal if using Supabase (no downtime)

-- ============================================================================
-- PHASE 1: PREPARATION AND VALIDATION
-- ============================================================================

BEGIN TRANSACTION;

-- Create backup columns in case we need to rollback
ALTER TABLE IF EXISTS public.buildings_full_merge_scanning
    ADD COLUMN IF NOT EXISTS bbl_legacy VARCHAR(10);

-- Copy current BBL values to backup before any changes
UPDATE public.buildings_full_merge_scanning
SET bbl_legacy = bbl
WHERE bbl_legacy IS NULL;

-- Validate BIN column has proper data
DO $$
DECLARE
    missing_bins INT;
    total_buildings INT;
BEGIN
    SELECT COUNT(*) INTO total_buildings FROM public.buildings_full_merge_scanning;
    SELECT COUNT(*) INTO missing_bins FROM public.buildings_full_merge_scanning
    WHERE bin IS NULL OR bin = '';

    RAISE NOTICE 'Pre-migration validation: % of % buildings have BINs',
        (total_buildings - missing_bins), total_buildings;

    IF missing_bins > 100 THEN
        RAISE EXCEPTION 'Too many missing BINs (%). Please run data cleaning first.', missing_bins;
    END IF;
END $$;

-- ============================================================================
-- PHASE 2: SCHEMA CHANGES - BIN BECOMES PRIMARY
-- ============================================================================

-- 2.1: Drop existing foreign key constraints that depend on BBL
-- (They will be recreated for BIN)
DO $$
BEGIN
    -- Drop constraints that reference buildings(bbl)
    EXECUTE 'ALTER TABLE IF EXISTS public.reference_images
             DROP CONSTRAINT IF EXISTS fk_reference_images_bbl CASCADE';
    EXECUTE 'ALTER TABLE IF EXISTS public.scans
             DROP CONSTRAINT IF EXISTS fk_scans_confirmed_bbl CASCADE';
    RAISE NOTICE 'Dropped foreign key constraints';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Warning during constraint drop: %', SQLERRM;
END $$;

-- 2.2: Convert BIN column to ensure it can handle both numbers and 'N/A' strings
ALTER TABLE public.buildings_full_merge_scanning
    ALTER COLUMN bin TYPE VARCHAR(10);

-- 2.3: Add NOT NULL constraint to BIN (NULL values should be rare after data cleaning)
-- First, set any remaining NULL/empty values to 'N/A' as placeholder
UPDATE public.buildings_full_merge_scanning
SET bin = 'N/A'
WHERE bin IS NULL OR bin = '' OR bin = 'nan';

-- Then add the constraint
ALTER TABLE public.buildings_full_merge_scanning
    ALTER COLUMN bin SET NOT NULL;

-- 2.4: Add UNIQUE constraint on BIN (except for N/A entries which can have multiples)
CREATE UNIQUE INDEX idx_buildings_bin_unique
ON public.buildings_full_merge_scanning(bin)
WHERE bin != 'N/A';

RAISE NOTICE 'Phase 2 complete: BIN column prepared';

-- ============================================================================
-- PHASE 3: FOREIGN KEY MIGRATION
-- ============================================================================

-- Drop the OLD primary key constraint (BBL was UNIQUE NOT NULL)
ALTER TABLE public.buildings_full_merge_scanning
    DROP CONSTRAINT IF EXISTS buildings_full_merge_scanning_bbl_key;

-- Now we can have multiple buildings with the same BBL (complex lots)
-- Remove the UNIQUE constraint from BBL
ALTER TABLE public.buildings_full_merge_scanning
    ALTER COLUMN bbl DROP NOT NULL;

-- Make BBL nullable (we'll keep it as a secondary identifier)
ALTER TABLE public.buildings_full_merge_scanning
    ALTER COLUMN bbl TYPE VARCHAR(10);

-- Add a regular index on BBL for lookups (not unique)
CREATE INDEX IF NOT EXISTS idx_buildings_bbl_secondary
ON public.buildings_full_merge_scanning(bbl);

-- 3.1: Update reference_images table to use BIN
-- First, check if BBL column exists and rename to BIN if needed
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'reference_images' AND column_name = 'BBL'
    ) THEN
        ALTER TABLE public.reference_images RENAME COLUMN "BBL" TO bin;
        RAISE NOTICE 'Renamed reference_images.BBL to bin';
    END IF;
END $$;

-- Ensure reference_images.bin exists
ALTER TABLE public.reference_images
    ADD COLUMN IF NOT EXISTS bin VARCHAR(10);

-- Populate bin from bbl if needed (in case data migration wasn't done)
UPDATE public.reference_images ri
SET bin = (
    SELECT b.bin FROM public.buildings_full_merge_scanning b
    WHERE b.bbl = ri.bbl LIMIT 1
)
WHERE ri.bin IS NULL AND ri.bbl IS NOT NULL;

-- Drop the old BBL column from reference_images if it still exists
ALTER TABLE public.reference_images
    DROP COLUMN IF EXISTS bbl CASCADE;

-- Add foreign key from reference_images.bin to buildings.bin
ALTER TABLE public.reference_images
    ADD CONSTRAINT fk_reference_images_bin
    FOREIGN KEY (bin) REFERENCES public.buildings_full_merge_scanning(bin)
    ON DELETE CASCADE;

RAISE NOTICE 'Phase 3a complete: reference_images migrated to BIN';

-- 3.2: Update scans table to use BIN
-- Rename BBL-related columns to BIN-related columns
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'scans' AND column_name = 'candidate_bbls'
    ) THEN
        ALTER TABLE public.scans RENAME COLUMN candidate_bbls TO candidate_bins;
        RAISE NOTICE 'Renamed scans.candidate_bbls to candidate_bins';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'scans' AND column_name = 'top_match_bbl'
    ) THEN
        ALTER TABLE public.scans RENAME COLUMN top_match_bbl TO top_match_bin;
        RAISE NOTICE 'Renamed scans.top_match_bbl to top_match_bin';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'scans' AND column_name = 'confirmed_bbl'
    ) THEN
        ALTER TABLE public.scans RENAME COLUMN confirmed_bbl TO confirmed_bin;
        RAISE NOTICE 'Renamed scans.confirmed_bbl to confirmed_bin';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Info: Column rename skipped (may not exist): %', SQLERRM;
END $$;

-- Ensure the columns exist in scans table
ALTER TABLE public.scans
    ADD COLUMN IF NOT EXISTS candidate_bins TEXT[],
    ADD COLUMN IF NOT EXISTS top_match_bin VARCHAR(10),
    ADD COLUMN IF NOT EXISTS confirmed_bin VARCHAR(10);

-- Populate BIN columns from BBL mappings if they're empty
-- (In case data migration wasn't fully done)
UPDATE public.scans s
SET top_match_bin = (
    SELECT b.bin FROM public.buildings_full_merge_scanning b
    WHERE b.bbl = s.top_match_bbl LIMIT 1
)
WHERE s.top_match_bin IS NULL AND s.top_match_bbl IS NOT NULL;

UPDATE public.scans s
SET confirmed_bin = (
    SELECT b.bin FROM public.buildings_full_merge_scanning b
    WHERE b.bbl = s.confirmed_bbl LIMIT 1
)
WHERE s.confirmed_bin IS NULL AND s.confirmed_bbl IS NOT NULL;

-- Drop old BBL columns from scans if they exist (keep as backup first)
ALTER TABLE public.scans
    ADD COLUMN IF NOT EXISTS top_match_bbl_legacy VARCHAR(10),
    ADD COLUMN IF NOT EXISTS confirmed_bbl_legacy VARCHAR(10),
    ADD COLUMN IF NOT EXISTS candidate_bbls_legacy TEXT[];

-- Copy old values to backup
UPDATE public.scans
SET
    top_match_bbl_legacy = top_match_bbl,
    confirmed_bbl_legacy = confirmed_bbl,
    candidate_bbls_legacy = candidate_bbls
WHERE top_match_bbl_legacy IS NULL;

-- Drop old BBL columns
ALTER TABLE public.scans
    DROP COLUMN IF EXISTS top_match_bbl CASCADE,
    DROP COLUMN IF EXISTS confirmed_bbl CASCADE,
    DROP COLUMN IF EXISTS candidate_bbls CASCADE;

-- Add foreign key from scans.confirmed_bin to buildings.bin
ALTER TABLE public.scans
    ADD CONSTRAINT fk_scans_confirmed_bin
    FOREIGN KEY (confirmed_bin) REFERENCES public.buildings_full_merge_scanning(bin)
    ON DELETE SET NULL;

RAISE NOTICE 'Phase 3b complete: scans table migrated to BIN';

-- ============================================================================
-- PHASE 4: INDEX UPDATES
-- ============================================================================

-- Drop old BBL indexes
DROP INDEX IF EXISTS idx_buildings_bbl;

-- Create/update BIN index (unique for non-public-space buildings)
CREATE INDEX IF NOT EXISTS idx_buildings_bin
ON public.buildings_full_merge_scanning(bin);

-- Keep BBL as secondary index for legacy queries and cross-referencing
CREATE INDEX IF NOT EXISTS idx_buildings_bbl_secondary
ON public.buildings_full_merge_scanning(bbl) WHERE bbl IS NOT NULL;

RAISE NOTICE 'Phase 4 complete: Indexes updated';

-- ============================================================================
-- PHASE 5: VALIDATION
-- ============================================================================

-- Validate the migration was successful
DO $$
DECLARE
    bins_with_real_values INT;
    bins_marked_na INT;
    still_missing INT;
    duplicate_bins INT;
    total_buildings INT;
BEGIN
    SELECT COUNT(*) INTO total_buildings FROM public.buildings_full_merge_scanning;
    SELECT COUNT(*) INTO bins_with_real_values FROM public.buildings_full_merge_scanning WHERE bin != 'N/A' AND bin IS NOT NULL;
    SELECT COUNT(*) INTO bins_marked_na FROM public.buildings_full_merge_scanning WHERE bin = 'N/A';
    SELECT COUNT(*) INTO still_missing FROM public.buildings_full_merge_scanning WHERE bin IS NULL;
    SELECT COUNT(*) INTO duplicate_bins FROM (
        SELECT bin FROM public.buildings_full_merge_scanning
        WHERE bin != 'N/A'
        GROUP BY bin HAVING COUNT(*) > 1
    ) t;

    RAISE NOTICE '═════════════════════════════════════════════════════════════';
    RAISE NOTICE 'MIGRATION VALIDATION RESULTS';
    RAISE NOTICE '═════════════════════════════════════════════════════════════';
    RAISE NOTICE 'Total buildings: %', total_buildings;
    RAISE NOTICE 'With real BINs: % (%.2f%%)', bins_with_real_values,
        (bins_with_real_values::FLOAT / total_buildings * 100);
    RAISE NOTICE 'Marked as N/A (public spaces): % (%.2f%%)', bins_marked_na,
        (bins_marked_na::FLOAT / total_buildings * 100);
    RAISE NOTICE 'Still missing BINs: % (%.2f%%)', still_missing,
        (still_missing::FLOAT / total_buildings * 100);
    RAISE NOTICE 'Duplicate BINs (complex lots): %', duplicate_bins;
    RAISE NOTICE '═════════════════════════════════════════════════════════════';

    IF still_missing > 100 THEN
        RAISE WARNING 'WARNING: % buildings still missing BINs', still_missing;
    END IF;
END $$;

COMMIT;

RAISE NOTICE '✅ Migration 004 complete! System now uses BIN as primary identifier.';
RAISE NOTICE '';
RAISE NOTICE 'NEXT STEPS:';
RAISE NOTICE '1. Update application code to use BIN instead of BBL';
RAISE NOTICE '2. Test all building lookups with real BINs';
RAISE NOTICE '3. Update R2 storage paths: reference/{bbl}/ → reference/{bin}/';
RAISE NOTICE '4. Update API documentation';
RAISE NOTICE '';
RAISE NOTICE 'ROLLBACK PROCEDURE (if needed):';
RAISE NOTICE 'The legacy columns bbl_legacy, candidate_bbls_legacy, etc. are preserved.';
RAISE NOTICE 'Contact support if rollback is needed.';
