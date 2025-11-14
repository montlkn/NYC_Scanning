# BIN Migration: Actual Execution Guide

**CRITICAL:** This is the step we skipped. This guide walks through actually getting the cleaned data INTO the database and testing end-to-end with real images.

---

## Step 1: Load Cleaned BIN Data Into Database

### Prerequisites
```bash
# 1. Verify cleaned data exists
ls -lh backend/data/final/full_dataset_fixed_bins.csv

# 2. Verify database is accessible
psql $DATABASE_URL -c "SELECT 1"

# 3. Have backup of current data ready
pg_dump $DATABASE_URL > backup_pre_bin_migration.sql
```

### Execute Ingestion

```bash
cd /Users/lucienmount/coding/nyc_scan/backend

# Install any missing dependencies
pip install pandas sqlalchemy aiosqlite

# Run ingestion script
python scripts/ingest_cleaned_bins.py
```

**Expected Output:**
```
[1/5] Backing up existing data...
✅ Backup created: buildings_backup_pre_bin_migration

[2/5] Clearing existing building data...
✅ Cleared existing building data

[3/5] Loading and preparing CSV data...
Loaded 37,237 rows from CSV
Prepared 37,192 buildings for insertion
  - Public spaces (N/A BIN): 17

[4/5] Inserting buildings into database...
  Inserted 5000 / 37192
  Inserted 10000 / 37192
  ... (continues)

[5/5] Verifying ingestion...
Total buildings in database: 37,192
BIN Distribution:
  - Valid BINs: 37,175 (99.95%)
  - Public spaces (N/A): 17

Sample buildings with valid BINs:
  BIN: 1001234, BBL: 1000001, Address: 123 Main Street
  ...

✅ INGESTION SUCCESSFUL - DATA READY FOR TESTING
```

### Verify Database State

```bash
# Check total buildings loaded
psql $DATABASE_URL -c "SELECT COUNT(*) as total FROM buildings_full_merge_scanning;"

# Check BIN distribution
psql $DATABASE_URL -c "
  SELECT
    COUNT(*) as total,
    COUNT(CASE WHEN bin != 'N/A' THEN 1 END) as with_bin,
    COUNT(CASE WHEN bin = 'N/A' THEN 1 END) as public_spaces
  FROM buildings_full_merge_scanning;
"

# Check a specific building by BIN
psql $DATABASE_URL -c "
  SELECT bin, bbl, address, latitude, longitude
  FROM buildings_full_merge_scanning
  WHERE bin = '1001234'
  LIMIT 1;
"

# Check public spaces
psql $DATABASE_URL -c "
  SELECT COUNT(*), address
  FROM buildings_full_merge_scanning
  WHERE bin = 'N/A'
  GROUP BY address
  LIMIT 5;
"
```

---

## Step 2: Verify R2 Storage Connection

### Test R2 Access

```bash
# 1. Check environment variables
echo "R2_ACCOUNT_ID: $R2_ACCOUNT_ID"
echo "R2_ACCESS_KEY_ID: $R2_ACCESS_KEY_ID"
echo "R2_BUCKET: $R2_BUCKET"
echo "R2_PUBLIC_URL: $R2_PUBLIC_URL"

# 2. Test S3 CLI (if installed)
aws s3 ls s3://$R2_BUCKET/reference/ --endpoint-url "https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com"

# 3. Check what reference images exist
aws s3 ls s3://$R2_BUCKET/reference/ --recursive --endpoint-url "https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com" | head -20

# 4. Check path format currently being used
aws s3 ls s3://$R2_BUCKET/reference/ --endpoint-url "https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com" | grep -E "reference/[0-9]"
```

### Test Python Connection

Create `test_r2_connection.py`:
```python
import asyncio
from utils.storage import get_r2_client

async def test_r2():
    try:
        client = get_r2_client()

        # List buckets
        response = client.list_buckets()
        print(f"✅ Connected to R2")
        print(f"Buckets: {[b['Name'] for b in response.get('Buckets', [])]}")

        # List reference images
        response = client.list_objects_v2(
            Bucket='building-images',
            Prefix='reference/'
        )
        count = len(response.get('Contents', []))
        print(f"Reference images in R2: {count}")

        # Show sample paths
        print("\nSample reference image paths:")
        for obj in response.get('Contents', [])[:5]:
            print(f"  {obj['Key']}")

    except Exception as e:
        print(f"❌ R2 Connection Failed: {e}")

asyncio.run(test_r2())
```

Run it:
```bash
python test_r2_connection.py
```

---

## Step 3: Run Database Migration Verification

```bash
# This verifies the schema changes and data integrity
python scripts/verify_migration.py
```

**Expected Output:**
```
=== Verifying Database Schema ===
✅ Primary key: BIN (correct)
✅ BIN column exists: character varying
✅ BBL column exists (secondary): character varying

=== Verifying Data Integrity ===
Total buildings: 37,192
Buildings with valid BINs: 37,175 (99.95%)
Buildings with public spaces: 17
✅ BIN coverage is acceptable (>99%)
✅ Public spaces properly marked
✅ No NULL BINs

=== Verifying Foreign Keys ===
✅ Reference images BIN foreign key: ...
✅ No orphaned reference images

=== Verifying Indexes ===
✅ Found 8 indexes
✅ BIN indexes present

=== MIGRATION VERIFICATION REPORT ===
✅ Passed: 12
❌ Failed: 0
⚠️  Warnings: 0

STATUS: ✅ MIGRATION VERIFIED SUCCESSFULLY
```

---

## Step 4: Test Core Functionality

### 4a: Query Building by BIN

```bash
# Create test_queries.py
python << 'EOF'
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select
from models.database import Building

async def test():
    from models.config import get_settings
    settings = get_settings()

    # Convert to async URL
    db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(db_url)

    async with AsyncSession(engine) as session:
        # Test 1: Query by BIN
        result = await session.execute(
            select(Building).where(Building.bin == '1001234').limit(1)
        )
        building = result.scalar_one()
        print(f"✅ Found by BIN: {building.bin} - {building.address}")

        # Test 2: Query by BBL (multiple results)
        result = await session.execute(
            select(Building).where(Building.bbl == '1000001')
        )
        buildings = result.scalars().all()
        print(f"✅ Found by BBL: {len(buildings)} buildings on lot 1000001")

        # Test 3: Query public spaces
        result = await session.execute(
            select(Building).where(Building.bin == 'N/A').limit(3)
        )
        public = result.scalars().all()
        print(f"✅ Found public spaces: {len(public)} examples")
        for b in public:
            print(f"   - {b.address}")

        # Test 4: Check scannable buildings
        result = await session.execute(
            select(Building).where(
                (Building.bin != 'N/A') &
                (Building.scan_enabled == True)
            ).limit(1)
        )
        scannable = result.scalar_one()
        print(f"✅ Scannable building: {scannable.bin} - {scannable.address}")

    await engine.dispose()

asyncio.run(test())
EOF
```

### 4b: Test Service Layer

```bash
python << 'EOF'
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from services.geospatial import get_buildings_in_radius
from models.config import get_settings

async def test():
    settings = get_settings()
    db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(db_url)

    async with AsyncSession(engine) as session:
        # Test radius search
        candidates = await get_buildings_in_radius(
            session,
            lat=40.7128,
            lng=-74.0060,
            radius_meters=500
        )

        print(f"✅ Radius search returned {len(candidates)} candidates")
        print("\nSample candidates (with BINs):")
        for c in candidates[:3]:
            print(f"  BIN: {c['bin']}, Distance: {c.get('distance', '?')}m, {c['address'][:40]}")

        # Verify no N/A BINs in results
        na_count = sum(1 for c in candidates if c['bin'] == 'N/A')
        if na_count == 0:
            print(f"\n✅ Correctly filtered out public spaces")
        else:
            print(f"\n❌ ERROR: Found {na_count} public spaces in results")

    await engine.dispose()

asyncio.run(test())
EOF
```

### 4c: Test API Endpoints

```bash
# Start the application
python -m uvicorn main:app --reload

# In another terminal, test endpoints:

# Get building by BIN
curl http://localhost:8000/buildings/1001234 | jq .

# Get nearby buildings
curl "http://localhost:8000/buildings/nearby?lat=40.7128&lng=-74.0060&radius_meters=500" | jq .

# Search buildings
curl "http://localhost:8000/buildings/search?q=Empire+State" | jq .
```

---

## Step 5: Test with Sample Images

### 5a: Prepare Sample Images

```bash
# Create sample test images directory
mkdir -p backend/test_images

# Download or copy sample building images:
# 1. Street view of a known building (e.g., Empire State Building)
# 2. Store in: backend/test_images/test_empire_state.jpg
```

### 5b: Test Scan Endpoint

```bash
curl -X POST http://localhost:8000/scan \
  -F "photo=@backend/test_images/test_empire_state.jpg" \
  -F "gps_lat=40.7484" \
  -F "gps_lng=-73.9857" \
  -F "compass_bearing=90" \
  -F "phone_pitch=0" \
  -F "phone_roll=0" | jq .
```

**Expected Response:**
```json
{
  "scan_id": "scan-12345",
  "matches": [
    {
      "bin": "1002456",
      "address": "350 5th Avenue",
      "confidence": 0.92,
      "distance": 15
    }
  ],
  "processing_time_ms": 1234,
  "debug_info": {
    "num_candidates": 45,
    "num_reference_images": 180,
    "num_matches": 3
  }
}
```

### 5c: Test Confirmation

```bash
curl -X POST http://localhost:8000/scans/scan-12345/confirm \
  -F "confirmed_bin=1002456"
```

---

## Step 6: Run Full Test Suite

```bash
# Install test dependencies
pip install -r tests/TEST_REQUIREMENTS.txt

# Run all tests
pytest tests/ -v --tb=short

# Run with coverage
pytest tests/ --cov=. --cov-report=html

# View coverage report
open htmlcov/index.html
```

**Target:** All 120 tests should pass ✅

---

## Step 7: Verify R2 Storage Paths

### Check Current State

```bash
# List images in R2
aws s3 ls s3://building-images/reference/ \
  --recursive \
  --endpoint-url "https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com" | head -20

# Check if using old BBL paths or new BIN paths
aws s3 ls s3://building-images/reference/ \
  --recursive \
  --endpoint-url "https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com" | grep "reference/" | head -5
```

### Migrate R2 Storage (if needed)

```bash
# Dry run first (no changes)
python scripts/migrate_r2_storage.py --dry-run --verbose

# If happy with results, run actual migration
python scripts/migrate_r2_storage.py --verbose

# Verify migration
aws s3 ls s3://building-images/reference/1001234/ \
  --endpoint-url "https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com"
```

---

## Troubleshooting

### Database Connection Issues

```bash
# Test connection
psql $DATABASE_URL -c "SELECT 1"

# Check credentials
echo "Host: $(echo $DATABASE_URL | cut -d'@' -f2 | cut -d':' -f1)"
echo "Port: $(echo $DATABASE_URL | cut -d':' -f4 | cut -d'/' -f1)"
echo "Database: $(echo $DATABASE_URL | cut -d'/' -f4)"
```

### R2 Connection Issues

```bash
# Test R2 credentials
aws configure list --profile r2

# Test S3 endpoint
aws s3 ls --endpoint-url "https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com"
```

### BIN Data Issues

```bash
# Find buildings with NULL or N/A bins
psql $DATABASE_URL -c "
  SELECT COUNT(*), bin
  FROM buildings_full_merge_scanning
  WHERE bin IS NULL OR bin = ''
  GROUP BY bin;
"

# Find public spaces that should not be scanned
psql $DATABASE_URL -c "
  SELECT COUNT(*), scan_enabled
  FROM buildings_full_merge_scanning
  WHERE bin = 'N/A'
  GROUP BY scan_enabled;
"
```

---

## Final Checklist

- [ ] BIN data loaded into database (37,192 buildings)
- [ ] Database verified: 99.95% BIN coverage
- [ ] R2 connection working
- [ ] Service layer queries working (BIN-based)
- [ ] API endpoints tested (returning BIN matches)
- [ ] Test suite passes (120/120 tests)
- [ ] Sample image scan tested
- [ ] R2 storage verified/migrated

---

## Next: Production Deployment

Once all above steps pass:

1. Create backup of production database
2. Run migration on production
3. Deploy updated application code
4. Monitor error rates
5. Run full test suite on production
6. Manual testing with real users
7. Update API documentation
8. Notify clients of changes

---

This is the ACTUAL execution. All previous work was preparation. This guide shows what needs to happen for real.
