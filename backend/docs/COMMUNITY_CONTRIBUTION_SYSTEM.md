# Community Contribution System - Complete Guide

**Wikipedia-style building verification with Pioneer rewards, edit suggestions, and source citations.**

---

## Table of Contents
1. [Overview](#overview)
2. [Pioneer Stamps System](#pioneer-stamps-system)
3. [Community Vetting](#community-vetting)
4. [Edit Suggestions](#edit-suggestions)
5. [Source Citations](#source-citations)
6. [API Reference](#api-reference)
7. [Frontend Integration](#frontend-integration)

---

## Overview

This system rewards users for contributing and verifying building information, creating a self-regulating Wikipedia-style database with reliability scoring.

### Key Features
- ✅ **Pioneer Stamps** - Reward users who contribute info for challenging buildings
- ✅ **Materials Fields** - Track mat_prim, mat_secondary, mat_tertiary (+5 XP each)
- ✅ **Community Vetting** - Users verify/dispute contributions
- ✅ **Edit Suggestions** - Propose corrections instead of disputing
- ✅ **Source Citations** - Link to Wikipedia, official sources, etc.
- ✅ **Reliability Scoring** - 0-1 score based on community consensus
- ✅ **Anti-Gaming** - Cannot verify own contributions
- ✅ **Auto-Accept** - Edits with 3+ votes and 2:1 ratio automatically accepted

---

## Pioneer Stamps System

### How It Works

**Standard Scan (Building in Top 3):**
```
User scans → Building matches → Confirms → +10 XP
```

**Pioneer Contribution (Building NOT in Top 3):**
```
User scans → Not in top 3 → Clicks "Not Here" → Contributes info → +60 XP + Stamps
```

### Contribution Fields

| Field | XP Value | Required |
|-------|----------|----------|
| Address | Base | Yes (for any contribution) |
| Architect | Base | No |
| Year Built | Base | No |
| Style | Base | No |
| Notes | Base | No |
| Primary Material | +5 XP | No |
| Secondary Material | +5 XP | No |
| Tertiary Material | +5 XP | No |

### XP Calculation

**Base XP:**
- 0 fields: 0 XP
- 1 field (address only): 10 XP
- 2 fields: 15 XP
- 3+ fields: 30 XP

**Materials Bonus:**
- +5 XP per material (up to +15 XP for all 3)

**Pioneer Bonus** (if NOT in top 3):
- +15 XP

**Maximum:** 30 + 15 + 15 = **60 XP**

### Stamps

| Stamp | Icon | Trigger | Bonus XP |
|-------|------|---------|----------|
| Pioneer | 🏆 | Contribute to non-top-3 building | - |
| Data Validator | 📍 | Any contribution | - |
| Master Validator | ⭐ | 10 Data Validator stamps | +50 |
| Database Legend | 👑 | 25 Data Validator stamps | +100 |
| Fact Checker | ✓ | 10 verifications | +25 |
| Truth Seeker | 🔍 | 50 verifications | +100 |

### Anti-Poisoning Protection

**Embeddings only stored if confirmed BIN is in top 3 matches.**

This prevents users from selecting wrong buildings and corrupting the CLIP model.

---

## Community Vetting

### Reliability Scoring

```
reliability_score = (verified / total) * confidence_multiplier

confidence_multiplier = min(total / 3, 1.0)
```

### Status Labels

| Score | Status | Badge Color | Display |
|-------|--------|-------------|---------|
| 0.9+ | Highly Verified | 🟢 Green | ✓✓ 5 users |
| 0.7-0.89 | Verified | 🔵 Blue | ✓ 3 users |
| 0.5-0.69 | Partially Verified | 🟠 Amber | ⚠ 2 users |
| 0-0.49 | Unverified | ⚪ Gray | 1 user |

### XP Rewards

- **Verify**: +5 XP
- **Dispute**: +3 XP
- **Vote on edit** (for): +2 XP
- **Vote on edit** (against): +1 XP
- **Propose edit**: +3 XP

---

## Edit Suggestions

### Why Edit Suggestions?

Instead of just disputing, users can **propose specific corrections** with reasons and sources.

### How It Works

1. User sees incorrect info
2. Clicks "Suggest Edit" instead of "Dispute"
3. Fills corrected fields + reason + optional source
4. Community votes on the suggestion
5. **Auto-accepts** at 3+ votes with 2:1 ratio

### Edit Suggestion Flow

```
User proposes edit
  ↓
+3 XP awarded
  ↓
Community votes (+2 XP per vote)
  ↓
If 3+ votes for AND 2:1 ratio:
  ↓
Auto-accepted
  ↓
Original contribution updated
  ↓
Suggester gets +10 XP bonus
```

### Voting Threshold

- **Minimum votes**: 3 votes "for"
- **Ratio**: 2:1 (e.g., 3 for / 1 against = auto-accept)
- **Examples**:
  - 3 for, 0 against → ✓ Accepted
  - 4 for, 1 against → ✓ Accepted
  - 3 for, 2 against → ✗ Still pending (ratio < 2:1)

---

## Source Citations

### Why Sources?

Wikipedia-style citations increase trust and allow users to verify information.

### Source Types

| Type | Description | Example |
|------|-------------|---------|
| `wikipedia` | Wikipedia article | https://en.wikipedia.org/wiki/Empire_State_Building |
| `official` | Official government/organization | NYC LPC designation report |
| `news` | News articles | NY Times architecture article |
| `other` | Other credible sources | Architectural database |

### Adding Sources

When contributing or proposing edits:
```json
{
  "address": "350 5th Ave",
  "architect": "Shreve, Lamb & Harmon",
  "source_url": "https://en.wikipedia.org/wiki/Empire_State_Building",
  "source_type": "wikipedia",
  "source_description": "Wikipedia article with detailed history"
}
```

### Display in UI

Show source badge near verified info:
```
📖 Wikipedia
🏛️ Official Source
📰 News Article
🔗 Source
```

---

## API Reference

### Contribution

**POST /api/scans/{scan_id}/confirm**
```json
{
  "confirmed_bin": "1234567",
  "user_id": "user_abc",
  "user_contributed_address": "350 5th Ave",
  "user_contributed_architect": "Shreve, Lamb & Harmon",
  "user_contributed_year_built": 1931,
  "user_contributed_style": "Art Deco",
  "user_contributed_notes": "Featured in King Kong",
  "user_contributed_mat_prim": "Limestone",
  "user_contributed_mat_secondary": "Steel",
  "user_contributed_mat_tertiary": "Aluminum"
}
```

**Response:**
```json
{
  "status": "confirmed",
  "was_in_top_3": false,
  "is_pioneer_contribution": true,
  "rewards": {
    "xp": 60,
    "stamps": [
      {"stamp_type": "pioneer", "is_new": true},
      {"stamp_type": "data_validator", "is_new": true}
    ],
    "message": "🏆 60 XP + 2 stamp(s)!"
  }
}
```

---

### Verification

**POST /api/contributions/{id}/verify**
```json
{
  "user_id": "user_abc",
  "verification_type": "verified"  // or "disputed"
}
```

**Response:**
```json
{
  "success": true,
  "verified_count": 5,
  "disputed_count": 1,
  "reliability_score": 0.83,
  "verification_status": "verified",
  "xp_earned": 5
}
```

---

### Edit Suggestions

**POST /api/contributions/{id}/suggest-edit**
```json
{
  "user_id": "user_def",
  "suggested_changes": {
    "architect": "Shreve, Lamb & Harmon Associates",
    "year_built": 1930
  },
  "reason": "Construction started in 1930, completed in 1931",
  "source_url": "https://en.wikipedia.org/wiki/Empire_State_Building"
}
```

**Response:**
```json
{
  "success": true,
  "suggestion_id": 42,
  "xp_earned": 3
}
```

**POST /api/edit-suggestions/{id}/vote**
```json
{
  "user_id": "user_xyz",
  "vote_type": "for"  // or "against"
}
```

**Response:**
```json
{
  "success": true,
  "votes_for": 3,
  "votes_against": 0,
  "auto_accepted": true,
  "xp_earned": 2
}
```

---

### Badges

**GET /api/contributions/{id}/badge**
```json
{
  "text": "✓ 5 users",
  "color": "#3b82f6",
  "icon": "✓",
  "description": "Verified by community",
  "verified_count": 5,
  "reliability_score": 0.83
}
```

---

## Frontend Integration

### 1. Display Verification Badge

```typescript
import { VerificationBadge } from '@/components/verification/VerificationBadge';

<VerificationBadge
  verifiedCount={contribution.verified_count}
  reliabilityScore={contribution.effective_reliability_score}
  onPress={() => setShowVerificationModal(true)}
/>
```

### 2. Allow Verification

```typescript
import { VerificationModal } from '@/components/verification/VerificationModal';

const handleVerify = async (contributionId: number, type: 'verified' | 'disputed') => {
  const response = await fetch(`${API_BASE}/contributions/${contributionId}/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: currentUser.id, verification_type: type }),
  });

  const result = await response.json();
  if (result.success) {
    Alert.alert('✓ Verified!', `You earned ${result.xp_earned} XP!`);
  }
};
```

### 3. Show Edit Suggestions

```typescript
// Get pending edits
const response = await fetch(`${API_BASE}/contributions/${contributionId}/edit-suggestions`);
const { suggestions } = await response.json();

// Display edit suggestions with vote buttons
suggestions.map(suggestion => (
  <EditSuggestionCard
    suggestion={suggestion}
    onVote={(voteType) => voteOnEdit(suggestion.id, voteType)}
  />
));
```

### 4. Display Source Citations

```typescript
{contribution.source_url && (
  <TouchableOpacity onPress={() => Linking.openURL(contribution.source_url)}>
    <View style={styles.sourceBadge}>
      <Text>{getSourceIcon(contribution.source_type)}</Text>
      <Text>{contribution.source_description || 'View Source'}</Text>
    </View>
  </TouchableOpacity>
)}
```

---

## Database Tables

### Core Tables

**building_contributions**
- Stores user contributions with materials
- Includes verification counts and reliability scores
- Has decay_factor and effective_reliability_score
- Links to source_url, source_type, source_description

**contribution_verifications**
- Tracks who verified/disputed what
- One row per user per contribution

**edit_suggestions**
- Pending, accepted, or rejected edits
- Includes reason and vote counts
- Auto-accepts at threshold

**edit_suggestion_votes**
- Tracks votes on edit suggestions
- One row per user per suggestion

---

## Best Practices

### For Users
- ✅ Provide sources when possible
- ✅ Propose edits instead of disputing when you know the correct info
- ✅ Vote on edit suggestions to help improve data
- ❌ Don't verify just for XP
- ❌ Don't propose frivolous edits

### For Developers
- Always show verification badges on user-contributed data
- Display effective_reliability_score (with decay) not just reliability_score
- Show edit suggestions alongside original data
- Make source citations clickable links
- Run decay cron job daily
- Monitor auto-accepted edits for quality

---

## Summary

✅ **Pioneer System** - Rewards users for contributing to challenging buildings
✅ **Materials Fields** - Track building materials (+5 XP each)
✅ **Community Vetting** - Wikipedia-style verification with reliability scoring
✅ **Edit Suggestions** - Propose corrections instead of just disputing
✅ **Source Citations** - Link to Wikipedia, official sources, etc.
✅ **Auto-Accept** - Community consensus automatically accepts good edits
✅ **Anti-Gaming** - Multiple safeguards prevent manipulation

**Key Innovation:** Users feel rewarded even when the system can't use their photo, creating a self-improving database!
