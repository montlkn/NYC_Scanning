# BIN Migration Guide

## Overview

This migration transitions the NYC Scan database from using BBL (Borough-Block-Lot) as the primary key to using an auto-incrementing ID with BIN (Building Identification Number) as the preferred unique identifier.

## Why This Migration?

### Problems with BBL as Primary Key:

1. **Not Unique**: 2,965 BBLs have multiple buildings (e.g., World Trade Center complex)
   - BBL `1000580001`: 7 different buildings
   - BBL `3018640014`: 5 different buildings with completely different addresses

2. **Duplicate WTC Buildings Lost**: During import, only 1 of 5 WTC buildings was kept due to UNIQUE constraint

3. **Visual Matching Ambiguity**: When user points at "3 World Trade Center", system can't distinguish it from "One World Trade Center" (same BBL)

### Solution: BIN-Based System

- **BIN Coverage**: 99.91% (37,205 of 37,237 buildings)
- **BIN is Unique**: Each building has its own BIN
- **ID Fallback**: 32 buildings without BIN use auto-incrementing ID

---

## Migration Steps

### Step 1: Deduplicate Source Data

**Problem**: Dataset has duplicate BINs (should be impossible):
- BIN `1088469.0` appears 2 times
- 5 other duplicate BINs found

**Solution**: Run deduplication script

```bash
cd backend

# Analyze duplicates
python scripts/deduplicate_buildings.py \
  --csv data/final/full_dataset.csv \
  --analyze-only

# Deduplicate (keeps best scoring row)
python scripts/deduplicate_buildings.py \
  --csv data/final/full_dataset.csv \
  --output data/final/full_dataset_clean.csv \
  --strategy keep_best
```

**What it does**:
- Identifies duplicate BINs
- Scores rows by data completeness (building_name, architect, style, etc.)
- Keeps highest-scoring row
- Outputs clean CSV

---

### Step 2: Run Database Migrations

**Prerequisites**:
- Deduplicated data imported to Supabase
- Database backup created

#### Migration 001: Add ID Primary Key

```bash
# Run in Supabase SQL Editor
psql $DATABASE_URL < backend/migrations/001_add_id_primary_key.sql
```

**What it does**:
1. Adds `id SERIAL` column
2. Drops `bbl` primary key constraint
3. Makes `id` the new primary key
4. Creates unique index on `bin` (where not null)
5. Creates regular index on `bbl` (allows duplicates)
6. Adds helper functions for lookups

#### Migration 002: Update Reference Images

```bash
# Run in Supabase SQL Editor
psql $DATABASE_URL < backend/migrations/002_update_reference_images.sql
```

**What it does**:
1. Adds `building_id` foreign key to reference_images
2. Populates `building_id` from existing BBL
3. Adds denormalized `bin` column for fast lookups
4. Creates indexes for performance
5. Adds helper functions for queries

---

### Step 3: Migrate Cloudflare R2 Cache

**Current cache structure**:
```
reference/{bbl}/{bearing}.jpg
```

**New cache structure**:
```
reference/BIN-{bin}/{bearing}.jpg     # 99.91% of buildings
reference/ID-{id}/{bearing}.jpg       # 0.09% without BIN
```

**Migration strategy**: Gradual migration (no downtime)

The updated `reference_images.py` service will:
1. Check new BIN-based cache path first
2. Fall back to old BBL-based path
3. Lazily copy from old → new path when accessed
4. Eventually delete old paths after 30 days

**No action required** - migration happens automatically during normal operation.

---

### Step 4: Verify Migration

```sql
-- Check ID assignment
SELECT COUNT(*), MIN(id), MAX(id)
FROM buildings_full_merge_scanning;

-- Check BIN uniqueness
SELECT bin, COUNT(*)
FROM buildings_full_merge_scanning
WHERE bin IS NOT NULL
GROUP BY bin
HAVING COUNT(*) > 1;
-- Should return 0 rows

-- Check buildings without BIN
SELECT id, bbl, address, borough
FROM buildings_full_merge_scanning
WHERE bin IS NULL;
-- Should return 32 rows

-- Check reference_images migration
SELECT
  COUNT(*) as total,
  COUNT(building_id) as with_building_id,
  COUNT(bin) as with_bin
FROM reference_images;
```

---

## Application Code Changes

### Updated Models (`backend/models/database.py`)

**Before**:
```python
class Building(Base):
    bbl = Column(String(10), primary_key=True)  # ❌ Not unique!
    bin = Column(String(7), index=True)
```

**After**:
```python
class Building(Base):
    id = Column(Integer, primary_key=True, autoincrement=True)  # ✅ Universal ID
    bbl = Column(String(10), index=True)  # NOT unique
    bin = Column(String(7), index=True)   # Unique where not null
```

### Cache Key Generation (`backend/services/reference_images.py`)

```python
def get_cache_key(building_id: int, bin: Optional[str], bbl: str, bearing: float) -> str:
    if bin:
        return f"reference/BIN-{bin}/{int(bearing)}.jpg"
    else:
        return f"reference/ID-{building_id}/{int(bearing)}.jpg"
```

### Database Queries

**Before**:
```python
building = await session.get(Building, bbl)
```

**After**:
```python
# By ID (preferred)
building = await session.get(Building, building_id)

# By BIN
building = await session.execute(
    select(Building).where(Building.bin == bin)
).scalar_one_or_none()

# By BBL (may return multiple - get best)
building = await session.execute(
    select(Building)
    .where(Building.bbl == bbl)
    .order_by(Building.walk_score.desc())
    .limit(1)
).scalar_one_or_none()
```

---

## Handling Edge Cases

### Buildings Without BIN (32 total)

Examples:
- Parks: Flushing Meadows Corona Park
- Infrastructure: Pier 55
- Duplicate BBLs: 5 buildings at BBL `3018640014`

**Strategy**: Use auto-incrementing ID
- Each gets unique ID
- Cache path: `reference/ID-{id}/{bearing}.jpg`
- Visual matching still works (compares photo to Street View)

### Duplicate BBLs (2,965 BBLs)

**Before migration**: Only 1 building per BBL could exist (UNIQUE constraint)
**After migration**: All buildings on shared lot are captured

Example - World Trade Center (BBL `1000580001`):
| Building | BIN | ID |
|----------|-----|-----|
| One World Trade Center | 1088469 | 1234 |
| 3 World Trade Center | 1088797 | 1235 |
| 4 World Trade Center | 1088795 | 1236 |
| 7 World Trade Center | 1088798 | 1237 |
| 175 Greenwich (3 WTC) | 1090954 | 1238 |

User scans any WTC building → GPS finds all 5 → CLIP picks correct one visually

---

## Rollback Plan

If migration fails:

```sql
-- Restore from backup
pg_restore -d $DATABASE_URL backup.dump

-- Or manually revert
ALTER TABLE buildings_full_merge_scanning DROP COLUMN id;
ALTER TABLE buildings_full_merge_scanning ADD PRIMARY KEY (bbl);
ALTER TABLE reference_images DROP COLUMN building_id;
ALTER TABLE reference_images DROP COLUMN bin;
```

**Important**: Rollback must happen before new data is inserted with the new schema.

---

## Testing Checklist

- [ ] Deduplication script removes duplicate BINs
- [ ] Migration 001 completes without errors
- [ ] Migration 002 completes without errors
- [ ] All buildings have unique ID
- [ ] BINs are unique (where not null)
- [ ] 32 buildings without BIN identified
- [ ] Reference images have building_id populated
- [ ] Cache key generation works for both BIN and ID
- [ ] Scan API returns results using new schema
- [ ] Visual matching works for multi-building BBLs (e.g., WTC)

---

## Timeline

1. **Day 1**: Run deduplication on local CSV
2. **Day 1**: Re-import clean data to Supabase
3. **Day 1**: Run migration 001 (add ID)
4. **Day 1**: Run migration 002 (update reference_images)
5. **Day 1**: Deploy updated application code
6. **Day 2-30**: Gradual cache migration (automatic)
7. **Day 30**: Clean up old BBL-based cache paths

---

## Post-Migration Benefits

✅ **Capture all buildings**: All 5 WTC buildings now in database
✅ **Unique identifiers**: BIN provides true uniqueness
✅ **Better visual matching**: Can distinguish buildings on same lot
✅ **Future-proof**: Handles edge cases (parks, infrastructure, etc.)
✅ **Backward compatible**: Old BBL queries still work
✅ **No downtime**: Cache migration is gradual

---

## Questions?

See the individual migration SQL files for detailed comments and implementation.
