# scripts/validate_metadata.py
import pandas as pd
import psycopg2
import os
from fuzzywuzzy import fuzz

SCAN_DB_URL = os.getenv('SCAN_DB_URL')

print("="*60)
print("METADATA VALIDATION REPORT")
print("="*60)

# Load landmarks CSV for BBL cross-reference
print("\nLoading NYC Landmarks dataset...")
landmarks_df = pd.read_csv('data/Individual_Landmark_and_Historic_District_Building_Database_20250918.csv')
landmarks_df['address_clean'] = landmarks_df['Des_Addres'].astype(str).str.lower().str.strip()
print(f"Loaded {len(landmarks_df)} landmarks")

# Connect to database
conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

# Get all buildings
cur.execute("""
    SELECT 
        id, bbl, des_addres, build_nme, arch_build, 
        date_combo, style_prim, style_sec, mat_prim, 
        hist_dist, borough, block, lot, bin
    FROM buildings
    WHERE tier = 1
    ORDER BY id
""")
buildings = cur.fetchall()

print(f"\n{'='*60}")
print(f"CHECKING {len(buildings)} BUILDINGS")
print(f"{'='*60}")

# Track issues
bbl_mismatches = []
missing_data = []

for row in buildings:
    building_id, bbl, address, name, architect, date, style_prim, style_sec, mat_prim, hist_dist, borough, block, lot, bin_num = row
    
    # Cross-reference BBL
    address_clean = address.lower().strip()
    
    # Find match in landmarks CSV
    landmarks_df['score'] = landmarks_df['address_clean'].apply(
        lambda x: fuzz.ratio(address_clean, str(x)) if pd.notna(x) else 0
    )
    best_match = landmarks_df.nlargest(1, 'score').iloc[0]
    
    if best_match['score'] >= 90:  # Good match
        csv_bbl = str(int(best_match['BBL'])) if pd.notna(best_match['BBL']) else None
        db_bbl = str(bbl) if bbl else None
        
        if csv_bbl != db_bbl:
            bbl_mismatches.append({
                'id': building_id,
                'address': address,
                'db_bbl': db_bbl,
                'csv_bbl': csv_bbl,
                'csv_address': best_match['Des_Addres']
            })
    
    # Check for missing data
    missing_fields = []
    if not bbl:
        missing_fields.append('bbl')
    if not architect:
        missing_fields.append('architect')
    if not date:
        missing_fields.append('date')
    if not style_prim:
        missing_fields.append('style_prim')
    if not mat_prim:
        missing_fields.append('mat_prim')
    
    if missing_fields:
        missing_data.append({
            'id': building_id,
            'address': address,
            'missing': missing_fields
        })

# Report BBL mismatches
print(f"\n{'='*60}")
print(f"BBL VALIDATION")
print(f"{'='*60}")
if bbl_mismatches:
    print(f"⚠️  Found {len(bbl_mismatches)} BBL mismatches:\n")
    for item in bbl_mismatches:
        print(f"  ID {item['id']}: {item['address']}")
        print(f"    DB BBL:  {item['db_bbl']}")
        print(f"    CSV BBL: {item['csv_bbl']} ({item['csv_address']})")
        print()
else:
    print("✅ All BBLs match!")

# Report missing data
print(f"\n{'='*60}")
print(f"MISSING DATA")
print(f"{'='*60}")
if missing_data:
    print(f"⚠️  Found {len(missing_data)} buildings with missing data:\n")
    for item in missing_data:
        print(f"  ID {item['id']}: {item['address']}")
        print(f"    Missing: {', '.join(item['missing'])}")
        print()
else:
    print("✅ All required fields populated!")

# Overall coverage statistics
cur.execute("""
    SELECT 
        COUNT(*) as total,
        COUNT(bbl) as with_bbl,
        COUNT(arch_build) as with_architect,
        COUNT(date_combo) as with_date,
        COUNT(style_prim) as with_style_prim,
        COUNT(style_sec) as with_style_sec,
        COUNT(mat_prim) as with_mat_prim,
        COUNT(hist_dist) as with_hist_dist,
        COUNT(build_nme) as with_name
    FROM buildings
    WHERE tier = 1
""")
stats = cur.fetchone()

pri