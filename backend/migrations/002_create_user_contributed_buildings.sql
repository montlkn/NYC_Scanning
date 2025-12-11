-- Migration: Create user_contributed_buildings table for crowdsourced building data
-- Purpose: Allow users to contribute additional metadata for buildings
-- BIN/BBL will be looked up from local PLUTO/BUILDING datasets based on GPS coordinates

CREATE TABLE IF NOT EXISTS user_contributed_buildings (
    id SERIAL PRIMARY KEY,

    -- Link to NYC building data (from PLUTO/BUILDING datasets)
    bin VARCHAR(10) NOT NULL,
    bbl VARCHAR(10),
    building_id INTEGER, -- FK to buildings_full_merge_scanning.id if already in main DB

    -- Address (from reverse geocoding + user confirmation)
    address TEXT NOT NULL,
    building_name TEXT,
    alternate_names TEXT[],

    -- Location (from user's GPS scan)
    gps_lat FLOAT NOT NULL,
    gps_lng FLOAT NOT NULL,
    gps_accuracy FLOAT,

    -- Building metadata (user-provided + enriched via Exa/web search)
    year_built INTEGER,
    architect TEXT,
    architectural_style TEXT,
    num_floors INTEGER,
    height_feet FLOAT,
    landmark_status TEXT, -- 'none', 'individual', 'historic_district', 'national_register'
    historic_district TEXT,
    building_use TEXT, -- residential, commercial, mixed, etc.
    notable_features TEXT, -- ornamental details, materials, etc.
    user_notes TEXT,

    -- Submission data
    submitted_by VARCHAR(36), -- user_id (nullable for anonymous contributions)
    initial_photo_url TEXT NOT NULL,
    initial_scan_id VARCHAR(36) REFERENCES scans(id),
    compass_bearing FLOAT,
    phone_pitch FLOAT,

    -- Verification/moderation status
    status VARCHAR(20) DEFAULT 'pending', -- 'pending', 'verified', 'rejected'
    verified_by VARCHAR(36), -- admin/moderator user_id
    verified_at TIMESTAMP,
    rejection_reason TEXT,

    -- Data enrichment pipeline
    enrichment_status VARCHAR(20) DEFAULT 'pending', -- 'pending', 'in_progress', 'completed', 'failed'
    enrichment_source TEXT, -- 'exa', 'google_places', 'wikipedia', 'manual'
    enrichment_data JSONB, -- Raw enrichment response data
    enrichment_confidence FLOAT,
    enrichment_completed_at TIMESTAMP,

    -- Street View auto-fetch
    street_view_images_fetched BOOLEAN DEFAULT FALSE,
    street_view_image_count INTEGER DEFAULT 0,
    street_view_fetch_attempted_at TIMESTAMP,

    -- Reference images tracking
    reference_image_count INTEGER DEFAULT 0,

    -- Community validation (optional - for future gamification)
    upvotes INTEGER DEFAULT 0,
    downvotes INTEGER DEFAULT 0,

    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX idx_ucb_bin ON user_contributed_buildings(bin);
CREATE INDEX idx_ucb_bbl ON user_contributed_buildings(bbl);
CREATE INDEX idx_ucb_building_id ON user_contributed_buildings(building_id);
CREATE INDEX idx_ucb_status ON user_contributed_buildings(status);
CREATE INDEX idx_ucb_enrichment_status ON user_contributed_buildings(enrichment_status);
CREATE INDEX idx_ucb_submitted_by ON user_contributed_buildings(submitted_by);
CREATE INDEX idx_ucb_created_at ON user_contributed_buildings(created_at);
CREATE INDEX idx_ucb_location ON user_contributed_buildings(gps_lat, gps_lng);

-- Auto-update timestamp trigger
CREATE OR REPLACE FUNCTION update_ucb_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ucb_update_timestamp
    BEFORE UPDATE ON user_contributed_buildings
    FOR EACH ROW
    EXECUTE FUNCTION update_ucb_timestamp();

-- Row Level Security
ALTER TABLE user_contributed_buildings ENABLE ROW LEVEL SECURITY;

-- Service role has full access
CREATE POLICY "service_all_ucb"
ON user_contributed_buildings FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- Authenticated users can view all contributions
CREATE POLICY "users_view_ucb"
ON user_contributed_buildings FOR SELECT
TO authenticated
USING (true);

-- Authenticated users can insert contributions
CREATE POLICY "users_insert_ucb"
ON user_contributed_buildings FOR INSERT
TO authenticated
WITH CHECK (true);

-- Users can update their own pending contributions
CREATE POLICY "users_update_own_ucb"
ON user_contributed_buildings FOR UPDATE
TO authenticated
USING (submitted_by = auth.uid()::text AND status = 'pending')
WITH CHECK (submitted_by = auth.uid()::text AND status = 'pending');

-- Stats view for analytics
CREATE OR REPLACE VIEW user_contribution_stats AS
SELECT
    COUNT(*) as total_contributions,
    COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending,
    COUNT(CASE WHEN status = 'verified' THEN 1 END) as verified,
    COUNT(CASE WHEN status = 'rejected' THEN 1 END) as rejected,
    COUNT(CASE WHEN enrichment_status = 'completed' THEN 1 END) as enriched,
    COUNT(CASE WHEN street_view_images_fetched = TRUE THEN 1 END) as with_street_view,
    AVG(reference_image_count) as avg_reference_images_per_contribution,
    COUNT(DISTINCT submitted_by) as unique_contributors,
    COUNT(DISTINCT bin) as unique_buildings_contributed,
    MIN(created_at) as first_contribution_date,
    MAX(created_at) as latest_contribution_date
FROM user_contributed_buildings;
