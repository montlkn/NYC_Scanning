-- ============================================================
-- MIGRATION 006: User Aesthetic Profiles
-- Run in Supabase SQL Editor
-- ============================================================

CREATE TABLE IF NOT EXISTS public.user_aesthetic_profiles (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    
    -- Raw scores (additive, pre-normalization)
    raw_scores JSONB DEFAULT '{"classicist":0,"romantic":0,"stylist":0,"modernist":0,"industrialist":0,"visionary":0,"pop_culturalist":0,"vernacularist":0,"austerist":0}',
    
    -- Normalized scores (0-100, sum to 100)
    normalized_scores JSONB DEFAULT '{"classicist":11.11,"romantic":11.11,"stylist":11.11,"modernist":11.11,"industrialist":11.11,"visionary":11.11,"pop_culturalist":11.11,"vernacularist":11.11,"austerist":11.11}',
    
    -- Metadata
    confidence DOUBLE PRECISION DEFAULT 10,
    action_counts JSONB DEFAULT '{}',
    
    -- Quiz
    onboarding_quiz_complete BOOLEAN DEFAULT FALSE,
    quiz_completed_at TIMESTAMPTZ,
    
    -- Timestamps
    last_updated TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- RLS
ALTER TABLE public.user_aesthetic_profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own profile" ON public.user_aesthetic_profiles
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users can update own profile" ON public.user_aesthetic_profiles
    FOR UPDATE USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own profile" ON public.user_aesthetic_profiles
    FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Index
CREATE INDEX IF NOT EXISTS idx_user_profiles_quiz ON public.user_aesthetic_profiles(onboarding_quiz_complete);

-- ============================================================
-- MIGRATION 007: User Aesthetic Events
-- ============================================================

CREATE TABLE IF NOT EXISTS public.user_aesthetic_events (
    event_uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    
    event_type TEXT NOT NULL,
    building_bbl TEXT,
    payload JSONB DEFAULT '{}',
    aesthetic_vector JSONB,
    
    base_weight DOUBLE PRECISION,
    final_weight DOUBLE PRECISION,
    
    processed BOOLEAN DEFAULT FALSE,
    processed_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    event_timestamp TIMESTAMPTZ DEFAULT NOW()
);

-- RLS
ALTER TABLE public.user_aesthetic_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own events" ON public.user_aesthetic_events
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own events" ON public.user_aesthetic_events
    FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_events_user ON public.user_aesthetic_events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_unprocessed ON public.user_aesthetic_events(user_id, processed) WHERE processed = FALSE;
CREATE INDEX IF NOT EXISTS idx_events_type ON public.user_aesthetic_events(event_type);

-- ============================================================
-- HELPER FUNCTION: Calculate alignment (cosine similarity)
-- ============================================================

CREATE OR REPLACE FUNCTION calculate_aesthetic_alignment(
    p_user_profile JSONB,
    p_building_profile JSONB
) RETURNS DOUBLE PRECISION AS $$
DECLARE
    archetypes TEXT[] := ARRAY['classicist','romantic','stylist','modernist','industrialist','visionary','pop_culturalist','vernacularist','austerist'];
    dot_product DOUBLE PRECISION := 0;
    user_magnitude DOUBLE PRECISION := 0;
    building_magnitude DOUBLE PRECISION := 0;
    u_val DOUBLE PRECISION;
    b_val DOUBLE PRECISION;
    arch TEXT;
BEGIN
    FOREACH arch IN ARRAY archetypes LOOP
        u_val := COALESCE((p_user_profile->>arch)::DOUBLE PRECISION, 0);
        b_val := COALESCE((p_building_profile->>arch)::DOUBLE PRECISION, 0);
        dot_product := dot_product + (u_val * b_val);
        user_magnitude := user_magnitude + (u_val * u_val);
        building_magnitude := building_magnitude + (b_val * b_val);
    END LOOP;
    
    IF user_magnitude = 0 OR building_magnitude = 0 THEN
        RETURN 0;
    END IF;
    
    RETURN dot_product / (sqrt(user_magnitude) * sqrt(building_magnitude));
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================
-- HELPER FUNCTION: Get recommended buildings for user
-- ============================================================

CREATE OR REPLACE FUNCTION get_recommended_buildings(
    p_user_id UUID,
    p_lat DOUBLE PRECISION DEFAULT NULL,
    p_lng DOUBLE PRECISION DEFAULT NULL,
    p_radius_m INTEGER DEFAULT 2000,
    p_limit INTEGER DEFAULT 50
) RETURNS TABLE (
    bbl TEXT,
    building_name TEXT,
    address TEXT,
    primary_aesthetic TEXT,
    final_score DOUBLE PRECISION,
    alignment_score DOUBLE PRECISION,
    recommendation_score DOUBLE PRECISION,
    distance_m DOUBLE PRECISION
) AS $$
DECLARE
    user_profile JSONB;
BEGIN
    -- Get user profile
    SELECT normalized_scores INTO user_profile
    FROM user_aesthetic_profiles
    WHERE user_id = p_user_id;
    
    IF user_profile IS NULL THEN
        user_profile := '{"classicist":11.11,"romantic":11.11,"stylist":11.11,"modernist":11.11,"industrialist":11.11,"visionary":11.11,"pop_culturalist":11.11,"vernacularist":11.11,"austerist":11.11}';
    END IF;
    
    RETURN QUERY
    SELECT 
        b.bbl::TEXT,
        b.build_nme::TEXT,
        b.des_addres::TEXT,
        b.primary_aesthetic,
        b.final_score,
        calculate_aesthetic_alignment(user_profile, b.normalized_profile) as alignment_score,
        (0.6 * calculate_aesthetic_alignment(user_profile, b.normalized_profile) + 0.4 * COALESCE(b.final_score, 0) / 100) as recommendation_score,
        CASE 
            WHEN p_lat IS NOT NULL AND p_lng IS NOT NULL THEN
                ST_Distance(
                    ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(b.geocoded_lng, b.geocoded_lat), 4326)::geography
                )
            ELSE NULL
        END as distance_m
    FROM buildings_full_merge_scanning b
    WHERE b.normalized_profile IS NOT NULL
        AND (p_lat IS NULL OR p_lng IS NULL OR 
            ST_DWithin(
                ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326)::geography,
                ST_SetSRID(ST_MakePoint(b.geocoded_lng, b.geocoded_lat), 4326)::geography,
                p_radius_m
            ))
    ORDER BY recommendation_score DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================
-- VERIFY
-- ============================================================
DO $$ BEGIN RAISE NOTICE '✅ Migrations 006-007 complete!'; END $$;
