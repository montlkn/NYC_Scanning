-- Migration: Add Vetting System for User Contributions
-- Purpose: Allow users to verify/vet building information contributed by others

-- 1. Add vetting columns to building_contributions table
ALTER TABLE building_contributions
ADD COLUMN IF NOT EXISTS verified_count INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS disputed_count INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS reliability_score FLOAT DEFAULT 0.0,
ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMP;

-- 2. Create contribution_verifications table (tracks who verified what)
CREATE TABLE IF NOT EXISTS contribution_verifications (
    id SERIAL PRIMARY KEY,
    contribution_id INTEGER NOT NULL REFERENCES building_contributions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    verification_type TEXT NOT NULL, -- 'verified' or 'disputed'
    verified_at TIMESTAMP DEFAULT NOW(),

    -- Prevent duplicate verifications
    CONSTRAINT unique_user_verification UNIQUE (contribution_id, user_id)
);

CREATE INDEX idx_contribution_verifications_contribution ON contribution_verifications(contribution_id);
CREATE INDEX idx_contribution_verifications_user ON contribution_verifications(user_id);
CREATE INDEX idx_contribution_verifications_type ON contribution_verifications(verification_type);

-- 3. Create function to calculate reliability score
CREATE OR REPLACE FUNCTION calculate_reliability_score(
    p_verified_count INTEGER,
    p_disputed_count INTEGER
)
RETURNS FLOAT AS $$
DECLARE
    v_total INTEGER;
    v_score FLOAT;
BEGIN
    v_total := p_verified_count + p_disputed_count;

    IF v_total = 0 THEN
        RETURN 0.0;
    END IF;

    -- Score = (verified / total) * confidence_multiplier
    -- Confidence increases with more total verifications
    v_score := (p_verified_count::FLOAT / v_total) * LEAST(v_total::FLOAT / 3.0, 1.0);

    RETURN v_score;
END;
$$ LANGUAGE plpgsql;

-- 4. Create function to verify/dispute a contribution
CREATE OR REPLACE FUNCTION verify_contribution(
    p_contribution_id INTEGER,
    p_user_id TEXT,
    p_verification_type TEXT, -- 'verified' or 'disputed'
    p_xp_reward INTEGER
)
RETURNS TABLE (
    success BOOLEAN,
    verified_count INTEGER,
    disputed_count INTEGER,
    reliability_score FLOAT,
    is_new BOOLEAN
) AS $$
DECLARE
    v_is_new BOOLEAN;
    v_verified_count INTEGER;
    v_disputed_count INTEGER;
    v_reliability_score FLOAT;
    v_contribution_user_id TEXT;
BEGIN
    -- Check if contribution exists and get its author
    SELECT user_id INTO v_contribution_user_id
    FROM building_contributions
    WHERE id = p_contribution_id;

    IF NOT FOUND THEN
        RETURN QUERY SELECT false, 0, 0, 0.0, false;
        RETURN;
    END IF;

    -- Prevent self-verification
    IF v_contribution_user_id = p_user_id THEN
        RETURN QUERY SELECT false, 0, 0, 0.0, false;
        RETURN;
    END IF;

    -- Insert or update verification
    INSERT INTO contribution_verifications (contribution_id, user_id, verification_type)
    VALUES (p_contribution_id, p_user_id, p_verification_type)
    ON CONFLICT (contribution_id, user_id) DO UPDATE SET
        verification_type = p_verification_type,
        verified_at = NOW()
    RETURNING (xmax = 0) INTO v_is_new; -- xmax = 0 means INSERT (new), otherwise UPDATE

    -- Count total verifications and disputes
    SELECT
        COUNT(*) FILTER (WHERE verification_type = 'verified'),
        COUNT(*) FILTER (WHERE verification_type = 'disputed')
    INTO v_verified_count, v_disputed_count
    FROM contribution_verifications
    WHERE contribution_id = p_contribution_id;

    -- Calculate reliability score
    v_reliability_score := calculate_reliability_score(v_verified_count, v_disputed_count);

    -- Update contribution record
    UPDATE building_contributions
    SET
        verified_count = v_verified_count,
        disputed_count = v_disputed_count,
        reliability_score = v_reliability_score,
        last_verified_at = NOW()
    WHERE id = p_contribution_id;

    -- Award XP to verifier if this is a new verification
    IF v_is_new THEN
        PERFORM update_user_achievements(p_user_id, p_xp_reward, 0, 0, 0);
    END IF;

    RETURN QUERY SELECT true, v_verified_count, v_disputed_count, v_reliability_score, v_is_new;
END;
$$ LANGUAGE plpgsql;

-- 5. Create view for verified contributions (reliability >= 0.7)
CREATE OR REPLACE VIEW verified_contributions AS
SELECT
    bc.*,
    CASE
        WHEN bc.reliability_score >= 0.9 THEN 'highly_verified'
        WHEN bc.reliability_score >= 0.7 THEN 'verified'
        WHEN bc.reliability_score >= 0.5 THEN 'partially_verified'
        ELSE 'unverified'
    END as verification_status
FROM building_contributions bc
WHERE bc.reliability_score >= 0.7
ORDER BY bc.reliability_score DESC, bc.verified_count DESC;

-- 6. Add vetting achievements to user_achievements table
ALTER TABLE user_achievements
ADD COLUMN IF NOT EXISTS total_verifications INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS total_disputes INTEGER DEFAULT 0;

-- 7. Update the update_user_achievements function to handle verifications
CREATE OR REPLACE FUNCTION update_user_achievements(
    p_user_id TEXT,
    p_xp_delta INTEGER DEFAULT 0,
    p_scan_delta INTEGER DEFAULT 0,
    p_confirmation_delta INTEGER DEFAULT 0,
    p_pioneer_delta INTEGER DEFAULT 0,
    p_verification_delta INTEGER DEFAULT 0,
    p_dispute_delta INTEGER DEFAULT 0
)
RETURNS void AS $$
BEGIN
    INSERT INTO user_achievements (
        user_id,
        total_xp,
        total_scans,
        total_confirmations,
        total_pioneer_contributions,
        total_verifications,
        total_disputes
    )
    VALUES (
        p_user_id,
        p_xp_delta,
        p_scan_delta,
        p_confirmation_delta,
        p_pioneer_delta,
        p_verification_delta,
        p_dispute_delta
    )
    ON CONFLICT (user_id) DO UPDATE SET
        total_xp = user_achievements.total_xp + p_xp_delta,
        total_scans = user_achievements.total_scans + p_scan_delta,
        total_confirmations = user_achievements.total_confirmations + p_confirmation_delta,
        total_pioneer_contributions = user_achievements.total_pioneer_contributions + p_pioneer_delta,
        total_verifications = user_achievements.total_verifications + p_verification_delta,
        total_disputes = user_achievements.total_disputes + p_dispute_delta,
        updated_at = NOW();
END;
$$ LANGUAGE plpgsql;

COMMENT ON TABLE contribution_verifications IS 'Tracks user verifications and disputes of building contributions';
COMMENT ON FUNCTION verify_contribution IS 'Records a user verification/dispute and updates reliability score';
COMMENT ON FUNCTION calculate_reliability_score IS 'Calculates reliability score (0-1) based on verifications vs disputes';
COMMENT ON VIEW verified_contributions IS 'Shows contributions with reliability >= 0.7';
