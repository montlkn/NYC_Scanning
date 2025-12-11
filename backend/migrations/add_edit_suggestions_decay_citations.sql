-- Migration: Add Edit Suggestions, Verification Decay, and Source Citations
-- Purpose: Allow users to propose edits, decay old verifications, add source citations

-- 1. Create edit_suggestions table
CREATE TABLE IF NOT EXISTS edit_suggestions (
    id SERIAL PRIMARY KEY,
    contribution_id INTEGER NOT NULL REFERENCES building_contributions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,

    -- Suggested changes (only include fields being changed)
    suggested_address TEXT,
    suggested_architect TEXT,
    suggested_year_built INTEGER,
    suggested_style TEXT,
    suggested_notes TEXT,
    suggested_mat_prim TEXT,
    suggested_mat_secondary TEXT,
    suggested_mat_tertiary TEXT,

    -- Metadata
    reason TEXT,  -- Why this edit is suggested
    status TEXT DEFAULT 'pending',  -- 'pending', 'accepted', 'rejected'
    votes_for INTEGER DEFAULT 0,
    votes_against INTEGER DEFAULT 0,

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),

    CONSTRAINT unique_user_edit_per_contribution UNIQUE (contribution_id, user_id)
);

CREATE INDEX idx_edit_suggestions_contribution ON edit_suggestions(contribution_id);
CREATE INDEX idx_edit_suggestions_user ON edit_suggestions(user_id);
CREATE INDEX idx_edit_suggestions_status ON edit_suggestions(status);

-- 2. Create edit_suggestion_votes table
CREATE TABLE IF NOT EXISTS edit_suggestion_votes (
    id SERIAL PRIMARY KEY,
    suggestion_id INTEGER NOT NULL REFERENCES edit_suggestions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    vote_type TEXT NOT NULL,  -- 'for' or 'against'
    voted_at TIMESTAMP DEFAULT NOW(),

    CONSTRAINT unique_user_vote_per_suggestion UNIQUE (suggestion_id, user_id)
);

CREATE INDEX idx_edit_suggestion_votes_suggestion ON edit_suggestion_votes(suggestion_id);
CREATE INDEX idx_edit_suggestion_votes_user ON edit_suggestion_votes(user_id);

-- 3. Add source citations to building_contributions
ALTER TABLE building_contributions
ADD COLUMN IF NOT EXISTS source_url TEXT,
ADD COLUMN IF NOT EXISTS source_type TEXT,  -- 'wikipedia', 'official', 'news', 'other'
ADD COLUMN IF NOT EXISTS source_description TEXT;

-- 4. Add verification decay fields
ALTER TABLE building_contributions
ADD COLUMN IF NOT EXISTS decay_factor FLOAT DEFAULT 1.0,
ADD COLUMN IF NOT EXISTS effective_reliability_score FLOAT DEFAULT 0.0;

-- 5. Create function to calculate verification decay
CREATE OR REPLACE FUNCTION calculate_verification_decay(
    p_created_at TIMESTAMP,
    p_last_verified_at TIMESTAMP
)
RETURNS FLOAT AS $$
DECLARE
    v_days_since_creation INTEGER;
    v_days_since_last_verification INTEGER;
    v_decay_factor FLOAT;
BEGIN
    -- Calculate days
    v_days_since_creation := EXTRACT(DAY FROM NOW() - p_created_at);
    v_days_since_last_verification := CASE
        WHEN p_last_verified_at IS NULL THEN v_days_since_creation
        ELSE EXTRACT(DAY FROM NOW() - p_last_verified_at)
    END;

    -- Decay formula:
    -- - No decay for first 90 days
    -- - After 90 days: decay factor = 1.0 - ((days - 90) / 365) * 0.5
    -- - Maximum 50% decay after 365 days of no verification

    IF v_days_since_last_verification < 90 THEN
        v_decay_factor := 1.0;
    ELSE
        v_decay_factor := GREATEST(0.5, 1.0 - ((v_days_since_last_verification - 90)::FLOAT / 365.0) * 0.5);
    END IF;

    RETURN v_decay_factor;
END;
$$ LANGUAGE plpgsql;

-- 6. Create function to update decay factors (run periodically)
CREATE OR REPLACE FUNCTION update_contribution_decay_factors()
RETURNS INTEGER AS $$
DECLARE
    v_updated_count INTEGER := 0;
BEGIN
    UPDATE building_contributions
    SET
        decay_factor = calculate_verification_decay(created_at, last_verified_at),
        effective_reliability_score = reliability_score * calculate_verification_decay(created_at, last_verified_at)
    WHERE reliability_score IS NOT NULL;

    GET DIAGNOSTICS v_updated_count = ROW_COUNT;
    RETURN v_updated_count;
END;
$$ LANGUAGE plpgsql;

-- 7. Create function to propose an edit
CREATE OR REPLACE FUNCTION propose_edit_suggestion(
    p_contribution_id INTEGER,
    p_user_id TEXT,
    p_suggested_changes JSONB,
    p_reason TEXT
)
RETURNS TABLE (
    suggestion_id INTEGER,
    success BOOLEAN
) AS $$
DECLARE
    v_suggestion_id INTEGER;
    v_contribution_user_id TEXT;
BEGIN
    -- Check if contribution exists and get its author
    SELECT user_id INTO v_contribution_user_id
    FROM building_contributions
    WHERE id = p_contribution_id;

    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::INTEGER, false;
        RETURN;
    END IF;

    -- Insert edit suggestion
    INSERT INTO edit_suggestions (
        contribution_id,
        user_id,
        suggested_address,
        suggested_architect,
        suggested_year_built,
        suggested_style,
        suggested_notes,
        suggested_mat_prim,
        suggested_mat_secondary,
        suggested_mat_tertiary,
        reason
    ) VALUES (
        p_contribution_id,
        p_user_id,
        p_suggested_changes->>'address',
        p_suggested_changes->>'architect',
        (p_suggested_changes->>'year_built')::INTEGER,
        p_suggested_changes->>'style',
        p_suggested_changes->>'notes',
        p_suggested_changes->>'mat_prim',
        p_suggested_changes->>'mat_secondary',
        p_suggested_changes->>'mat_tertiary',
        p_reason
    )
    ON CONFLICT (contribution_id, user_id) DO UPDATE SET
        suggested_address = EXCLUDED.suggested_address,
        suggested_architect = EXCLUDED.suggested_architect,
        suggested_year_built = EXCLUDED.suggested_year_built,
        suggested_style = EXCLUDED.suggested_style,
        suggested_notes = EXCLUDED.suggested_notes,
        suggested_mat_prim = EXCLUDED.suggested_mat_prim,
        suggested_mat_secondary = EXCLUDED.suggested_mat_secondary,
        suggested_mat_tertiary = EXCLUDED.suggested_mat_tertiary,
        reason = EXCLUDED.reason,
        updated_at = NOW()
    RETURNING id INTO v_suggestion_id;

    RETURN QUERY SELECT v_suggestion_id, true;
END;
$$ LANGUAGE plpgsql;

-- 8. Create function to vote on edit suggestion
CREATE OR REPLACE FUNCTION vote_on_edit_suggestion(
    p_suggestion_id INTEGER,
    p_user_id TEXT,
    p_vote_type TEXT  -- 'for' or 'against'
)
RETURNS TABLE (
    votes_for INTEGER,
    votes_against INTEGER,
    auto_accepted BOOLEAN
) AS $$
DECLARE
    v_votes_for INTEGER;
    v_votes_against INTEGER;
    v_auto_accepted BOOLEAN := false;
BEGIN
    -- Insert or update vote
    INSERT INTO edit_suggestion_votes (suggestion_id, user_id, vote_type)
    VALUES (p_suggestion_id, p_user_id, p_vote_type)
    ON CONFLICT (suggestion_id, user_id) DO UPDATE SET
        vote_type = EXCLUDED.vote_type,
        voted_at = NOW();

    -- Count votes
    SELECT
        COUNT(*) FILTER (WHERE vote_type = 'for'),
        COUNT(*) FILTER (WHERE vote_type = 'against')
    INTO v_votes_for, v_votes_against
    FROM edit_suggestion_votes
    WHERE suggestion_id = p_suggestion_id;

    -- Update suggestion vote counts
    UPDATE edit_suggestions
    SET
        votes_for = v_votes_for,
        votes_against = v_votes_against
    WHERE id = p_suggestion_id;

    -- Auto-accept if 3+ votes for and 2x more votes for than against
    IF v_votes_for >= 3 AND v_votes_for >= (v_votes_against * 2) THEN
        UPDATE edit_suggestions
        SET status = 'accepted'
        WHERE id = p_suggestion_id;
        v_auto_accepted := true;
    END IF;

    RETURN QUERY SELECT v_votes_for, v_votes_against, v_auto_accepted;
END;
$$ LANGUAGE plpgsql;

-- 9. Create view for pending edit suggestions
CREATE OR REPLACE VIEW pending_edit_suggestions AS
SELECT
    es.*,
    bc.address as current_address,
    bc.architect as current_architect,
    bc.year_built as current_year_built,
    bc.style as current_style,
    bc.confirmed_bin
FROM edit_suggestions es
JOIN building_contributions bc ON es.contribution_id = bc.id
WHERE es.status = 'pending'
ORDER BY es.votes_for DESC, es.created_at DESC;

-- 10. Update user_achievements to track edit suggestions
ALTER TABLE user_achievements
ADD COLUMN IF NOT EXISTS total_edit_suggestions INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS accepted_edit_suggestions INTEGER DEFAULT 0;

COMMENT ON TABLE edit_suggestions IS 'User-proposed edits to building contributions';
COMMENT ON TABLE edit_suggestion_votes IS 'Community votes on edit suggestions';
COMMENT ON FUNCTION calculate_verification_decay IS 'Calculates decay factor based on age and last verification';
COMMENT ON FUNCTION update_contribution_decay_factors IS 'Updates decay factors for all contributions (run daily)';
COMMENT ON FUNCTION propose_edit_suggestion IS 'Creates or updates an edit suggestion for a contribution';
COMMENT ON FUNCTION vote_on_edit_suggestion IS 'Records vote on edit suggestion and auto-accepts if threshold reached';
