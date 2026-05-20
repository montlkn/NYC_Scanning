-- Migration: Create scans and scan_feedback tables for analytics
-- Purpose: Track user scans, confirmations, and feedback for model improvement

-- Create scans table
CREATE TABLE IF NOT EXISTS scans (
    id VARCHAR(36) PRIMARY KEY,

    -- User (nullable for anonymous scans)
    user_id VARCHAR(36),

    -- User photo
    user_photo_url TEXT NOT NULL,
    thumbnail_url TEXT,

    -- GPS & sensor data
    gps_lat FLOAT NOT NULL,
    gps_lng FLOAT NOT NULL,
    gps_accuracy FLOAT,
    compass_bearing FLOAT NOT NULL,
    phone_pitch FLOAT DEFAULT 0,
    phone_roll FLOAT DEFAULT 0,

    -- Matching results (using BIN)
    candidate_bins TEXT[],
    candidate_scores JSONB,
    top_match_bin VARCHAR(10),
    top_confidence FLOAT,

    -- User confirmation
    confirmed_bin VARCHAR(10),
    was_correct BOOLEAN,
    confirmation_time_ms INTEGER,

    -- Performance metrics
    processing_time_ms INTEGER,
    num_candidates INTEGER,
    geospatial_query_ms INTEGER,
    image_fetch_ms INTEGER,
    clip_comparison_ms INTEGER,

    -- Error handling
    error_message TEXT,
    error_type VARCHAR(50),

    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    confirmed_at TIMESTAMP
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_scans_user_id ON scans(user_id);
CREATE INDEX IF NOT EXISTS idx_scans_confirmed_bin ON scans(confirmed_bin);
CREATE INDEX IF NOT EXISTS idx_scans_created_at ON scans(created_at);

-- Create scan_feedback table
CREATE TABLE IF NOT EXISTS scan_feedback (
    id SERIAL PRIMARY KEY,
    scan_id VARCHAR(36) REFERENCES scans(id) ON DELETE CASCADE,

    -- Feedback
    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
    feedback_text TEXT,
    feedback_type VARCHAR(20),

    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW()
);

-- Create index for feedback lookups
CREATE INDEX IF NOT EXISTS idx_scan_feedback_scan_id ON scan_feedback(scan_id);

-- Add foreign key constraint to buildings table (if exists)
-- Note: This is commented out because the buildings_full_merge_scanning table
-- might not have the exact structure we expect. Uncomment if you want strict FK.
-- ALTER TABLE scans
-- ADD CONSTRAINT fk_scans_confirmed_bin
-- FOREIGN KEY (confirmed_bin)
-- REFERENCES buildings_full_merge_scanning(bin);

-- Create cache_stats table (for monitoring reference image caching)
CREATE TABLE IF NOT EXISTS cache_stats (
    id SERIAL PRIMARY KEY,

    -- Cache metrics
    date TIMESTAMP,
    total_images INTEGER,
    images_fetched_today INTEGER,
    cache_hit_rate FLOAT,
    avg_fetch_time_ms FLOAT,
    total_cost_usd FLOAT,

    -- Source breakdown
    street_view_count INTEGER,
    mapillary_count INTEGER,
    user_upload_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_cache_stats_date ON cache_stats(date);

-- Grant permissions (adjust based on your Supabase setup)
-- Replace 'anon' and 'authenticated' with your actual roles if different
ALTER TABLE scans ENABLE ROW LEVEL SECURITY;
ALTER TABLE scan_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE cache_stats ENABLE ROW LEVEL SECURITY;

-- Allow service role to do everything
CREATE POLICY "Service role can do everything on scans"
ON scans FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

CREATE POLICY "Service role can do everything on scan_feedback"
ON scan_feedback FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

CREATE POLICY "Service role can do everything on cache_stats"
ON cache_stats FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- Optional: Allow authenticated users to view their own scans
CREATE POLICY "Users can view their own scans"
ON scans FOR SELECT
TO authenticated
USING (user_id = auth.uid()::text);

-- Optional: Allow authenticated users to submit feedback
CREATE POLICY "Users can submit feedback"
ON scan_feedback FOR INSERT
TO authenticated
WITH CHECK (true);
