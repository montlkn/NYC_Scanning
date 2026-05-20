-- NYC Scan Backend - Database Migration 005
-- Adds aesthetic profiling columns for 9-archetype scoring system
-- Run this on your Supabase SQL Editor

-- ============================================================================
-- ADD AESTHETIC PROFILING COLUMNS
-- ============================================================================

-- Primary and secondary aesthetic categories
ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS primary_aesthetic TEXT;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS secondary_aesthetic TEXT;

-- Individual archetype scores (raw weighted scores)
ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS classicist_score DOUBLE PRECISION DEFAULT 0;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS romantic_score DOUBLE PRECISION DEFAULT 0;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS stylist_score DOUBLE PRECISION DEFAULT 0;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS modernist_score DOUBLE PRECISION DEFAULT 0;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS industrialist_score DOUBLE PRECISION DEFAULT 0;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS visionary_score DOUBLE PRECISION DEFAULT 0;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS pop_culturalist_score DOUBLE PRECISION DEFAULT 0;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS vernacularist_score DOUBLE PRECISION DEFAULT 0;

ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS austerist_score DOUBLE PRECISION DEFAULT 0;

-- Normalized profile as JSONB (0-100 scale for each archetype)
-- Used for cosine similarity calculations with user profiles
ALTER TABLE public.buildings_full_merge_scanning 
ADD COLUMN IF NOT EXISTS normalized_profile JSONB;

-- ============================================================================
-- INDEXES FOR AESTHETIC QUERIES
-- ============================================================================

-- Index on primary aesthetic for filtering walks by style
CREATE INDEX IF NOT EXISTS idx_buildings_primary_aesthetic 
ON public.buildings_full_merge_scanning(primary_aesthetic);

-- Index on secondary aesthetic
CREATE INDEX IF NOT EXISTS idx_buildings_secondary_aesthetic 
ON public.buildings_full_merge_scanning(secondary_aesthetic);

-- Composite index for aesthetic + score queries
CREATE INDEX IF NOT EXISTS idx_buildings_aesthetic_score 
ON public.buildings_full_merge_scanning(primary_aesthetic, final_score DESC NULLS LAST);

-- GIN index on normalized_profile for JSONB queries
CREATE INDEX IF NOT EXISTS idx_buildings_normalized_profile 
ON public.buildings_full_merge_scanning USING GIN(normalized_profile);

-- ============================================================================
-- VERIFY MIGRATION
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 005 completed successfully!';
    RAISE NOTICE '';
    RAISE NOTICE '🎨 Added aesthetic columns:';
    RAISE NOTICE '   - primary_aesthetic (TEXT)';
    RAISE NOTICE '   - secondary_aesthetic (TEXT)';
    RAISE NOTICE '   - classicist_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - romantic_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - stylist_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - modernist_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - industrialist_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - visionary_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - pop_culturalist_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - vernacularist_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - austerist_score (DOUBLE PRECISION)';
    RAISE NOTICE '   - normalized_profile (JSONB)';
    RAISE NOTICE '';
    RAISE NOTICE '📋 Next: Run update_supabase_aesthetics.py to populate data';
END $$;
