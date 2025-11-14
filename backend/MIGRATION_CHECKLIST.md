# BBL to BIN Migration - Execution Checklist

## Pre-Migration Verification

- [ ] Review `MIGRATION_BBL_TO_BIN.md` - understand the full plan
- [ ] Review `MIGRATION_PROGRESS.md` - understand what's completed
- [ ] Review this checklist - understand what's remaining
- [ ] Backup your Supabase database
- [ ] Ensure you have a staging environment for testing

## Phase 1: Data Preparation ✅
**Status: COMPLETE**

- [x] Created `fix_bin_data.py` - BIN analysis script
- [x] Created `fix_bin_data_apply.py` - BIN fixing script
- [x] Analyzed 37,237 buildings
- [x] Auto-fixed 17 public spaces (parks/piers) → 'N/A'
- [x] Generated `full_dataset_fixed_bins.csv` - cleaned dataset
- [x] Generated `bin_analysis_report.txt` - detailed statistics
- [x] Generated `bin_fixes_template.csv` - research template
- [x] Achieved 99.88% BIN coverage

**Deliverables Ready:**
- ✅ `data/final/full_dataset_fixed_bins.csv`
- ✅ `data/final/bin_analysis_report.txt`
- ✅ `data/final/bin_changes_applied.csv`
- ✅ `data/final/bin_fixes_template.csv`

## Phase 2: Database Schema ✅
**Status: COMPLETE**

- [x] Created `migrations/004_migrate_bbl_to_bin.sql`
- [x] SQL includes:
  - [x] Primary key swap (BBL → BIN)
  - [x] Foreign key updates
  - [x] Column renames in reference_images and scans
  - [x] Index creation
  - [x] Backup columns for rollback
  - [x] Full validation logic
  - [x] Error handling

**To Deploy:**
- [ ] Review SQL script thoroughly
- [ ] Test on staging database first
- [ ] Run SQL migration on production

## Phase 3: Application Models ✅
**Status: COMPLETE**

- [x] Updated `models/database.py`:
  - [x] Building.bin is now primary key
  - [x] Building.bbl is now secondary (nullable)
  - [x] ReferenceImage.bin (was .BBL)
  - [x] Scan.candidate_bins (was .candidate_bbls)
  - [x] Scan.top_match_bin (was .top_match_bbl)
  - [x] Scan.confirmed_bin (was .confirmed_bbl)

**Committed:** ✅

## Phase 4: Service Layer ⏳
**Status: NOT STARTED**

### Task 1: Update `services/geospatial.py`
- [ ] Review current implementation
- [ ] Update `get_candidate_buildings()` function:
  - [ ] Change parameter from `bbl` to `bin`
  - [ ] Update building queries to use BIN
  - [ ] Update return dictionary keys: `'bbl'` → `'bin'`
  - [ ] Update candidate scoring logic
- [ ] Update all other functions using BBL
- [ ] Test with sample data
- [ ] Update docstrings

### Task 2: Update `services/reference_images.py`
- [ ] Review current implementation
- [ ] Update function parameters: BBL → BIN
- [ ] Update database queries to use bin column
- [ ] **Critical**: Update R2 storage paths:
  - Old: `reference/{bbl}/{angle}.jpg`
  - New: `reference/{bin}/{angle}.jpg`
- [ ] Update all building lookups
- [ ] Test image retrieval
- [ ] Update docstrings

### Task 3: Review `services/clip_matcher.py` (if applicable)
- [ ] Check if file exists and needs updates
- [ ] Update building matching logic if necessary

## Phase 5: API Routers ⏳
**Status: NOT STARTED**

### Task 1: Update `routers/scan.py`
- [ ] Review current implementation
- [ ] Update parameters: `confirmed_bbl` → `confirmed_bin`
- [ ] Update scan storage to use new BIN fields
- [ ] Update endpoint request/response objects
- [ ] Test scan confirmation flow
- [ ] Update docstrings and comments

### Task 2: Update `routers/buildings.py`
- [ ] Review current implementation
- [ ] Update endpoint parameters: `/buildings/{bbl}` → `/buildings/{bin}`
- [ ] Update function signatures
- [ ] Update building queries to use BIN
- [ ] Consider keeping legacy `/buildings/{bbl}` with deprecation notice:
  - [ ] Add deprecation warning header
  - [ ] Log usage
  - [ ] Document sunset date
- [ ] Test all endpoints
- [ ] Update docstrings

### Task 3: Update `routers/scan_phase1.py`
- [ ] Review Phase 1 database structure
- [ ] Update Phase 1 queries to use BIN if applicable
- [ ] Test Phase 1 scanning
- [ ] Update response objects

## Phase 6: Storage Migration ⏳
**Status: NOT STARTED**

### Task 1: Create `scripts/migrate_r2_storage.py`
- [ ] Create script to:
  - [ ] List all objects in reference/ folder
  - [ ] Check if using old BBL path format
  - [ ] Look up BIN for each BBL
  - [ ] Copy to new BIN path
  - [ ] Update metadata if needed
  - [ ] Delete old path when verified
  - [ ] Log all operations
  - [ ] Report completion statistics

### Task 2: Run R2 Migration
- [ ] Test on staging environment first
- [ ] Verify migration completeness
- [ ] Spot-check random files in new location
- [ ] Confirm old paths are cleaned up

## Phase 7: Testing & Deployment ⏳
**Status: NOT STARTED**

### Unit Tests
- [ ] Test Building model with BIN as primary key
- [ ] Test ReferenceImage.bin foreign key
- [ ] Test Scan model with new fields
- [ ] Test geospatial queries by BIN
- [ ] Test building lookups by BIN vs BBL

### Integration Tests
- [ ] Test complete scan workflow with BIN
- [ ] Test multiple buildings on same BBL:
  - [ ] Verify both buildings are returned
  - [ ] Verify correct building can be confirmed
- [ ] Test reference image retrieval by BIN
- [ ] Test scan confirmation and saving
- [ ] Test legacy BBL endpoint (if maintained)

### Error Handling Tests
- [ ] Test querying by 'N/A' BIN (public spaces)
- [ ] Test missing BIN handling
- [ ] Test duplicate BIN resolution
- [ ] Test invalid BIN errors

### Performance Tests
- [ ] BIN lookup performance
- [ ] Geospatial queries with new indexes
- [ ] Reference image query performance
- [ ] Scan storage/retrieval performance
- [ ] Database query optimization

### Staging Environment Testing
- [ ] Deploy to staging environment
- [ ] Run full test suite
- [ ] Manual testing of key workflows
- [ ] Load testing if applicable
- [ ] Security testing

### Production Deployment
- [ ] Deploy database migration
- [ ] Deploy application code
- [ ] Monitor error logs
- [ ] Monitor performance metrics
- [ ] Be ready for rollback if needed
- [ ] Update API documentation
- [ ] Notify users of endpoint changes

## Post-Migration Tasks

### Documentation
- [ ] Update API documentation
- [ ] Update database schema documentation
- [ ] Create upgrade guide for clients
- [ ] Document deprecation of old BBL endpoints (if any)

### Monitoring
- [ ] Monitor error rates for first 24 hours
- [ ] Monitor query performance
- [ ] Monitor storage usage
- [ ] Set up alerts for common errors

### Cleanup (Later)
- [ ] Once BBL endpoints deprecated, remove them
- [ ] Remove legacy backup columns (bbl_legacy, etc.) from database
- [ ] Archive old data if needed

### Optional Improvements
- [ ] Research and fill in the 28 missing BINs
- [ ] Improve R2 storage organization
- [ ] Add BIN validation to data ingestion pipeline

## Quick Reference: Files to Update

**Phase 4 (Services):**
- `backend/services/geospatial.py`
- `backend/services/reference_images.py`
- `backend/services/clip_matcher.py` (if applicable)

**Phase 5 (Routers):**
- `backend/routers/scan.py`
- `backend/routers/buildings.py`
- `backend/routers/scan_phase1.py`

**Phase 6 (Storage):**
- `backend/scripts/migrate_r2_storage.py` (new file)

**Phase 7 (Testing):**
- All test files for above modules

## Quick Reference: Database Changes

**Table: buildings_full_merge_scanning**
```
Before: bbl (String(10), PRIMARY KEY)
After:  bin (String(10), PRIMARY KEY)
        bbl (String(10), nullable, indexed)
```

**Table: reference_images**
```
Before: BBL (String(10), column name uppercase)
After:  bin (String(10), foreign key to buildings)
```

**Table: scans**
```
Before: candidate_bbls, top_match_bbl, confirmed_bbl
After:  candidate_bins, top_match_bin, confirmed_bin
```

## Estimated Timeline

| Phase | Duration | Status |
|-------|----------|--------|
| 1: Data Prep | 30 min | ✅ Done |
| 2: Database | 10 min | ✅ Done |
| 3: Models | 15 min | ✅ Done |
| 4: Services | 1-2 hours | ⏳ Next |
| 5: Routers | 1-2 hours | ⏳ Pending |
| 6: Storage | 30-60 min | ⏳ Pending |
| 7: Testing | 2-4 hours | ⏳ Pending |
| **Total** | **5-10 hours** | 🔄 In Progress |

## Rollback Plan

If something goes wrong:

1. **Database Rollback:**
   ```sql
   -- Restore from backup columns (if within transaction)
   UPDATE buildings_full_merge_scanning
   SET bbl = bbl_legacy WHERE bbl_legacy IS NOT NULL;
   ```

2. **Application Rollback:**
   ```bash
   git revert <migration-commit>
   deploy-application
   ```

3. **Data Rollback:**
   ```bash
   # Restore from full_dataset.csv (pre-migration)
   python scripts/restore_original_data.py
   ```

## Contact & Support

- Technical Questions: See `MIGRATION_BBL_TO_BIN.md`
- Progress Tracking: See `MIGRATION_PROGRESS.md`
- Data Issues: Check `data/final/bin_analysis_report.txt`
- Schema Issues: Review `migrations/004_migrate_bbl_to_bin.sql`

## Sign-Off

- [ ] All tasks completed
- [ ] All tests passing
- [ ] Documentation updated
- [ ] Deployed to production
- [ ] Monitoring in place
- [ ] Migration considered complete

---

**Started:** November 13, 2025
**Completion Target:** November 14, 2025
**Status:** 🔄 Phase 4 Ready to Start
