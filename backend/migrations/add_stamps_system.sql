-- Migration: Add Stamps and User Contributions System
-- Purpose: Track pioneer contributions, stamps, and user-submitted building data

-- 1. Create stamps table
CREATE TABLE IF NOT EXISTS user_stamps (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    stamp_type TEXT NOT NULL,  -- 'pioneer', 'data_validator', 'master_validator', 'database_legend'
    stamp_name TEXT NOT NULL,  -- Display name: "Pioneer", "Data Validator", etc.
    stamp_icon TEXT,           -- Emoji or icon identifier: "🏆", "📍", etc.
    awarded_at TIMESTAMP DEFAULT NOW(),
    scan_id TEXT,              -- Optional: Which scan earned this stamp
    metadata JSONB,            -- Additional data (contribution details, etc.)

    CONSTRAINT unique_user_stamp UNIQUE (user_id, stamp_type, scan_id)
);

CREATE INDEX idx_user_stamps_user_id ON user_stamps(user_id);
CREATE INDEX idx_user_stamps_stamp_type ON user_stamps(stamp_type);
CREATE INDEX idx_user_stamps_awarded_at ON user_stamps(awarded_at);

-- 2. Create building_contributions table
CREATE TABLE IF NOT EXISTS building_contributions (
    id SERIAL PRIMARY KEY,
    scan_id TEXT NOT NULL,
    user_id TEXT,
    confirmed_bin TEXT NOT NULL,

    -- Contributed fields
    address TEXT,
    architect TEXT,
    year_built INTEGER,
    style TEXT,
    notes TEXT,
    mat_prim TEXT,
    mat_secondary TEXT,
    mat_tertiary TEXT,

    -- Metadata
    contribution_type TEXT,    -- 'address_only', 'partial', 'full'
    was_in_top_3 BOOLEAN DEFAULT false,
    xp_awarded INTEGER DEFAULT 0,
    stamps_awarded TEXT[],     -- Array of stamp types awarded

    created_at TIMESTAMP DEFAULT NOW(),

    CONSTRAINT fk_scan FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
);

CREATE INDEX idx_building_contributions_user_id ON building_contributions(user_id);
CREATE INDEX idx_building_contributions_bin ON building_contributions(confirmed_bin);
CREATE INDEX idx_building_contributions_created_at ON building_contributions(created_at);

-- 3. Create user_achievements table (for tracking progress)
CREATE TABLE IF NOT EXISTS user_achievements (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,

    -- Stats
    total_scans INTEGER DEFAULT 0,
    total_confirmations INTEGER DEFAULT 0,
    total_pioneer_contributions INTEGER DEFAULT 0,
    total_xp INTEGER DEFAULT 0,

    -- Stamp counts
    pioneer_stamps INTEGER DEFAULT 0,
    data_validator_stamps INTEGER DEFAULT 0,

    -- Achievements unlocked
    achievements TEXT[],

    updated_at TIMESTAMP DEFAULT NOW(),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_user_achievements_user_id ON user_achievements(user_id);
CREATE INDEX idx_user_achievements_total_xp ON user_achievements(total_xp);

-- 4. Add contribution fields to scans table (if not already present)
ALTER TABLE scans
ADD COLUMN IF NOT EXISTS user_contributed_address TEXT,
ADD COLUMN IF NOT EXISTS user_contributed_architect TEXT,
ADD COLUMN IF NOT EXISTS user_contributed_year_built INTEGER,
ADD COLUMN IF NOT EXISTS user_contributed_style TEXT,
ADD COLUMN IF NOT EXISTS user_contributed_notes TEXT,
ADD COLUMN IF NOT EXISTS user_contributed_mat_prim TEXT,
ADD COLUMN IF NOT EXISTS user_contributed_mat_secondary TEXT,
ADD COLUMN IF NOT EXISTS user_contributed_mat_tertiary TEXT;

-- 5. Create function to award stamp
CREATE OR REPLACE FUNCTION award_stamp(
    p_user_id TEXT,
    p_stamp_type TEXT,
    p_stamp_name TEXT,
    p_stamp_icon TEXT,
    p_scan_id TEXT DEFAULT NULL,
    p_metadata JSONB DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    stamp_id INTEGER,
    is_new BOOLEAN
) AS $$
DECLARE
    v_stamp_id INTEGER;
    v_is_new BOOLEAN;
BEGIN
    -- Try to insert, ignore if duplicate
    INSERT INTO user_stamps (user_id, stamp_type, stamp_name, stamp_icon, scan_id, metadata)
    VALUES (p_user_id, p_stamp_type, p_stamp_name, p_stamp_icon, p_scan_id, p_metadata)
    ON CONFLICT (user_id, stamp_type, scan_id) DO NOTHING
    RETURNING id INTO v_stamp_id;

    IF v_stamp_id IS NOT NULL THEN
        v_is_new := true;

        -- Update user_achievements stamp count
        IF p_stamp_type = 'pioneer' THEN
            UPDATE user_achievements
            SET pioneer_stamps = pioneer_stamps + 1
            WHERE user_id = p_user_id;
        ELSIF p_stamp_type = 'data_validator' THEN
            UPDATE user_achievements
            SET data_validator_stamps = data_validator_stamps + 1
            WHERE user_id = p_user_id;
        END IF;
    ELSE
        v_is_new := false;
        -- Get existing stamp ID
        SELECT id INTO v_stamp_id
        FROM user_stamps
        WHERE user_id = p_user_id
          AND stamp_type = p_stamp_type
          AND (scan_id = p_scan_id OR (scan_id IS NULL AND p_scan_id IS NULL));
    END IF;

    RETURN QUERY SELECT v_stamp_id, v_is_new;
END;
$$ LANGUAGE plpgsql;

-- 6. Create function to update user achievements
CREATE OR REPLACE FUNCTION update_user_achievements(
    p_user_id TEXT,
    p_xp_delta INTEGER DEFAULT 0,
    p_scan_delta INTEGER DEFAULT 0,
    p_confirmation_delta INTEGER DEFAULT 0,
    p_pioneer_delta INTEGER DEFAULT 0
)
RETURNS void AS $$
BEGIN
    INSERT INTO user_achievements (user_id, total_xp, total_scans, total_confirmations, total_pioneer_contributions)
    VALUES (p_user_id, p_xp_delta, p_scan_delta, p_confirmation_delta, p_pioneer_delta)
    ON CONFLICT (user_id) DO UPDATE SET
        total_xp = user_achievements.total_xp + p_xp_delta,
        total_scans = user_achievements.total_scans + p_scan_delta,
        total_confirmations = user_achievements.total_confirmations + p_confirmation_delta,
        total_pioneer_contributions = user_achievements.total_pioneer_contributions + p_pioneer_delta,
        updated_at = NOW();
END;
$$ LANGUAGE plpgsql;

-- 7. Create view for user stamps leaderboard
CREATE OR REPLACE VIEW stamps_leaderboard AS
SELECT
    ua.user_id,
    ua.total_xp,
    ua.total_pioneer_contributions,
    ua.pioneer_stamps,
    ua.data_validator_stamps,
    ua.pioneer_stamps + ua.data_validator_stamps as total_stamps,
    CASE
        WHEN ua.data_validator_stamps >= 25 THEN 'Database Legend'
        WHEN ua.data_validator_stamps >= 10 THEN 'Master Validator'
        WHEN ua.pioneer_stamps >= 1 THEN 'Pioneer'
        ELSE 'Explorer'
    END as rank
FROM user_achievements ua
ORDER BY total_stamps DESC, ua.total_xp DESC;

COMMENT ON TABLE user_stamps IS 'Tracks stamps awarded to users for various contributions';
COMMENT ON TABLE building_contributions IS 'Stores user-contributed building data (address, architect, year, etc.)';
COMMENT ON TABLE user_achievements IS 'Aggregated user stats and achievement tracking';
COMMENT ON FUNCTION award_stamp IS 'Awards a stamp to a user and updates achievement counts';
COMMENT ON FUNCTION update_user_achievements IS 'Updates user achievement stats (XP, scans, contributions)';
