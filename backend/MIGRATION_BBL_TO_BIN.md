# NYC Scan Backend: BBL to BIN Migration Guide

## Overview

This document outlines the complete migration from BBL (Borough-Block-Lot) to BIN (Building Identification Number) as the primary building identifier in the NYC Scan backend.

**Migration Status:** 🔄 In Progress (Phase 2/5)

## Why Migrate?

- **Problem**: Multiple buildings can exist on a single BBL (complex lots)
- **Solution**: BIN uniquely identifies individual buildings
- **Coverage**: 99.88% of dataset has valid BINs
  - 37,192 buildings with real BINs
  - 17 public spaces marked as 'N/A' (parks, piers, etc.)
  - 28 buildings still missing BINs (researched but not found)

## Migration Phases

### ✅ Phase 1: Data Preparation (COMPLETED)

**What was done:**
1. Created `fix_bin_data.py` - BIN analysis and quality assessment script
2. Created `fix_bin_data_apply.py` - Automated fixing script
3. Auto-marked 17 public spaces as 'N/A' (can't have building identification numbers)
4. Generated cleaning template for remaining missing BINs

**Output files generated:**
- `data/final/full_dataset_fixed_bins.csv` - Cleaned dataset ready for migration
- `data/final/bin_analysis_report.txt` - Detailed analysis
- `data/final/bin_fixes_template.csv` - Template for manual BIN research
- `data/final/bin_changes_applied.csv` - Log of all auto-fixes

**Next step for this phase:** Research and fill in the 28 remaining missing BINs using NYC DOB BIS API

### ✅ Phase 2: Database Schema (COMPLETED)

**What was done:**
1. Created comprehensive migration SQL script: `migrations/004_migrate_bbl_to_bin.sql`
2. Script handles:
   - Converting BIN column from String(7) to String(10) to support 'N/A' values
   - Dropping old BBL-based foreign keys
   - Creating new BIN-based foreign keys
   - Updating indexes and constraints
   - Preserving backup columns (bbl_legacy, etc.) for potential rollback
   - Full validation and reporting

**Key changes in SQL migration:**
```sql
-- Before
buildings.bbl (String(10), PRIMARY KEY, UNIQUE)
buildings.bin (String(7), indexed but secondary)

-- After
buildings.bin (String(10), PRIMARY KEY, NOT NULL, UNIQUE except for 'N/A')
buildings.bbl (String(10), indexed but nullable, allows duplicates)
```

**Database tables affected:**
- `buildings_full_merge_scanning` - Primary table (swapped keys)
- `reference_images` - Column renamed: BBL → bin
- `scans` - Columns renamed: candidate_bbls → candidate_bins, top_match_bbl → top_match_bin, confirmed_bbl → confirmed_bin

### ✅ Phase 3: Application Models (COMPLETED)

**File updated:** `backend/models/database.py`

**Changes:**
1. **Building model**
   - `bbl` was primary key → now `bin` is primary key
   - `bbl` is now nullable secondary identifier (allows duplicates)
   - Can now represent multiple buildings on same lot

2. **ReferenceImage model**
   - Column renamed: `BBL` → `bin`
   - Foreign key now references `buildings.bin` instead of `buildings.bbl`

3. **Scan model**
   - `candidate_bbls` → `candidate_bins`
   - `top_match_bbl` → `top_match_bin`
   - `confirmed_bbl` → `confirmed_bin`
   - Foreign key now references `buildings.bin`

### 🔄 Phase 4: Service Layer (IN PROGRESS)

**Files to update:**

#### 1. `services/geospatial.py`
- `get_candidate_buildings()` - Update queries and return values to use BIN
- Lines 159-200: Return `'bin'` in dictionary instead of `'bbl'`
- Update candidate scoring to work with BINs

#### 2. `services/reference_images.py`
- `get_or_fetch_reference_image(bin)` - Change parameter from BBL to BIN
- `get_reference_images_for_candidates()` - Update to use BIN list
- `get_all_reference_images_for_building()` - Change lookup to use BIN
- **Important:** Update R2 storage paths:
  - Old: `reference/{bbl}/{angle}.jpg`
  - New: `reference/{bin}/{angle}.jpg`

#### 3. `services/clip_matcher.py` (if applicable)
- Update matching logic to work with BINs
- Update building lookups

### 🔄 Phase 5: API Routers (PENDING)

**Files to update:**

#### 1. `routers/scan.py`
- Update endpoint parameters to use BIN instead of BBL
- Change `confirmed_bbl` parameter to `confirmed_bin`
- Update scan storage to use new BIN fields

#### 2. `routers/buildings.py`
- Change endpoint: `/buildings/{bbl}` → `/buildings/{bin}`
- Update `get_building_detail(bin)` function signature
- Update `get_building_images(bin)` function
- **Option:** Keep legacy `/buildings/{bbl}` endpoint for backwards compatibility (with deprecation warning)

#### 3. `routers/scan_phase1.py`
- Update Phase 1 database queries to use BIN if applicable
- Update response objects to use BIN

### 🔄 Phase 6: Storage Migration (PENDING)

**Files to create:**

#### 1. `scripts/migrate_r2_storage.py`
```python
# Pseudocode for R2 migration script
For each reference image in R2:
  1. Check if using old BBL-based path: reference/{bbl}/{angle}.jpg
  2. Look up BIN for that BBL
  3. Copy to new BIN path: reference/{bin}/{angle}.jpg
  4. Update metadata
  5. Delete old path when complete
```

### ⏳ Phase 7: Testing & Deployment (PENDING)

**Checklist:**
- [ ] Test on staging environment first
- [ ] Verify all building lookups work with BIN
- [ ] Test scanning workflow with BIN
- [ ] Test multiple buildings on same BBL (complex lots)
- [ ] Verify R2 storage migration complete
- [ ] Performance testing
- [ ] Error handling for missing BINs ('N/A' values)

## Migration Scripts Created

### 1. `scripts/fix_bin_data.py`
**Purpose:** Analyze and identify BIN issues

**Usage:**
```bash
python scripts/fix_bin_data.py
```

**Output:**
- Console report with BIN coverage statistics
- `data/final/bin_analysis_report.txt` - Full analysis
- `data/final/bin_fixes_template.csv` - Manual correction template

### 2. `scripts/fix_bin_data_apply.py`
**Purpose:** Apply BIN fixes to dataset

**Usage:**
```bash
# Auto-fix public spaces and apply manual fixes
python scripts/fix_bin_data_apply.py
```

**Features:**
- Auto-marks public spaces as 'N/A'
- Applies manual fixes from template
- Validates results
- Generates change report

### 3. `migrations/004_migrate_bbl_to_bin.sql`
**Purpose:** Database schema migration

**Usage:**
```bash
# Run on Supabase via SQL Editor or via CLI
psql supabase_connection < migrations/004_migrate_bbl_to_bin.sql
```

**Safety features:**
- Wrapped in transaction (can rollback if issues)
- Preserves legacy columns for recovery
- Full validation before and after
- Clear error messages

## Data Quality Summary

**Before Migration:**
- 37,237 total buildings
- 37,205 with BINs (99.91%)
- 32 missing BINs (0.09%)
- 80 duplicate BINs (mostly placeholder values)

**After Auto-fix:**
- 37,237 total buildings
- 37,192 with real BINs (99.88%)
- 17 marked as 'N/A' (public spaces)
- 28 still missing BINs (0.08%)
- 82 duplicate BINs (legitimate complex lots)

## Known Limitations

1. **28 Missing BINs**
   - Not in NYC DOB database
   - May be: temporary structures, private buildings not registered, or data errors
   - Need manual research via NYC DOB BIS: https://a810-dobnow.nyc.gov/

2. **Public Spaces Marked 'N/A'**
   - Parks, piers, etc. don't have BINs by definition
   - Can be queried but won't match scanning images
   - Consider flagging as `scan_enabled=FALSE` in building table

3. **Legacy Code**
   - Some code may still reference BBL
   - Need comprehensive search & replace in codebase
   - Maintain backwards compatibility layer if needed

## Rollback Procedure

If migration needs to be reversed:

1. **Database rollback:**
   ```sql
   BEGIN TRANSACTION;

   -- Restore from backup columns
   UPDATE buildings_full_merge_scanning
   SET bbl = bbl_legacy
   WHERE bbl_legacy IS NOT NULL;

   -- Restore old foreign keys
   -- (Use backup tables if available)

   ROLLBACK;
   ```

2. **Code rollback:**
   - Git revert to pre-migration commit
   - Re-deploy previous version

## Testing Checklist

### Unit Tests to Create/Update

- [ ] Test Building model with BIN primary key
- [ ] Test ReferenceImage.bin foreign key
- [ ] Test Scan model with new BIN fields
- [ ] Test geospatial queries by BIN
- [ ] Test building lookup by BIN vs BBL

### Integration Tests

- [ ] Test full scan workflow with BIN
- [ ] Test multiple buildings on same BBL
- [ ] Test reference image retrieval by BIN
- [ ] Test scan confirmation with BIN
- [ ] Error handling for 'N/A' BINs

### Performance Tests

- [ ] BIN lookup performance vs BBL
- [ ] Geospatial queries with new indexes
- [ ] Reference image queries
- [ ] Scan storage/retrieval

## Documentation Updates Needed

- [ ] API documentation (BIN endpoints)
- [ ] Database schema documentation
- [ ] Developer guide for new BIN-based queries
- [ ] Migration notes for deployed systems
- [ ] Deprecation notices for old BBL endpoints

## Timeline Estimate

| Phase | Duration | Status |
|-------|----------|--------|
| Phase 1: Data Prep | 30 min | ✅ Done |
| Phase 2: Database | 5-10 min | ✅ Done |
| Phase 3: Models | 15 min | ✅ Done |
| Phase 4: Services | 1-2 hours | 🔄 In Progress |
| Phase 5: Routers | 1-2 hours | ⏳ Pending |
| Phase 6: Storage | 30-60 min | ⏳ Pending |
| Phase 7: Testing | 2-4 hours | ⏳ Pending |
| **Total** | **5-10 hours** | 🔄 In Progress |

## Contact & Support

For questions or issues during migration:
1. Check `data/final/bin_analysis_report.txt` for data issues
2. Review SQL migration for database errors
3. Check git history for model changes
4. Test thoroughly on staging before production

---

**Last Updated:** November 13, 2025
**Migration Status:** 🔄 Phase 4/7 (Services)
