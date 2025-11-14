# scripts/update_with_full_metadata.py
import pandas as pd
import psycopg2
import os
import numpy as np

SCAN_DB_URL = os.getenv('SCAN_DB_URL')

print('Loading BBL mappings with metadata...')
df = pd.read_csv('data/bbl_mappings.csv')
print(f'Loaded {len(df)} buildings')

# Clean addresses
df['address_clean'] = df['Des_Addres'].str.lower().str.strip()

conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

# Get current buildings
cur.execute('SELECT id, des_addres FROM buildings')
current = cur.fetchall()
print(f'Found {len(current)} buildings in scan database')

# Helper function to convert numpy types to Python types
def safe_int(val):
    if pd.isna(val):
        return None
    return int(val)

def safe_str(val):
    if pd.isna(val):
        return None
    return str(val)

matched = 0
not_matched = []

for building_id, address in current:
    address_clean = address.lower().strip()
    
    # Find match
    match = df[df['address_clean'] == address_clean]
    
    if len(match) > 0:
        row = match.iloc[0]
        
        # Update with ALL metadata
        cur.execute("""
            UPDATE buildings SET
                bbl = %s,
                bin = %s,
                borough = %s,
                block = %s,
                lot = %s,
                date_low = %s,
                date_high = %s,
                date_combo = %s,
                alt_date_1 = %s,
                alt_date_2 = %s,
                arch_build = %s,
                own_devel = %s,
                alt_arch_1 = %s,
                alt_arch_2 = %s,
                altered = %s,
                style_sec = %s,
                style_oth = %s,
                mat_prim = %s,
                mat_sec = %s,
                mat_third = %s,
                mat_four = %s,
                mat_other = %s,
                use_orig = %s,
                use_other = %s,
                build_type = %s,
                build_oth = %s,
                notes = %s,
                hist_dist = %s,
                short_bio = %s
            WHERE id = %s
        """, (
            safe_str(row['BBL']),
            safe_int(row['BIN']),
            safe_str(row.get('Borough')),
            safe_int(row.get('Block')),
            safe_int(row.get('Lot')),
            safe_int(row.get('Date_Low')),
            safe_int(row.get('Date_High')),
            safe_str(row.get('Date_Combo')),
            safe_str(row.get('Alt_Date_1')),
            safe_str(row.get('Alt_Date_2')),
            safe_str(row.get('Arch_Build')),
            safe_str(row.get('Own_Devel')),
            safe_str(row.get('Alt_Arch_1')),
            safe_str(row.get('Alt_Arch_2')),
            safe_int(row.get('Altered')),
            safe_str(row.get('Style_Sec')),
            safe_str(row.get('Style_Oth')),
            safe_str(row.get('Mat_Prim')),
            safe_str(row.get('Mat_Sec')),
            safe_str(row.get('Mat_Third')),
            safe_str(row.get('Mat_Four')),
            safe_str(row.get('Mat_Other')),
            safe_str(row.get('Use_Orig')),
            safe_str(row.get('Use_Other')),
            safe_str(row.get('Build_Type')),
            safe_str(row.get('Build_Oth')),
            safe_str(row.get('Notes')),
            safe_str(row.get('Hist_Dist')),
            safe_str(row.get('short_bio')),
            building_id
        ))
        matched += 1
        if matched % 10 == 0:
            print(f'  Matched {matched}...')
    else:
        not_matched.append((building_id, address))

conn.commit()
print(f'\nMatched: {matched}/{len(current)}')
print(f'Not matched: {len(not_matched)}')

if not_matched:
    print('\nBuildings without match:')
    for bid, addr in not_matched[:10]:
        print(f'  ID {bid}: {addr}')

# Verify metadata coverage
cur.execute("""
    SELECT 
        COUNT(*) as total,
        COUNT(bbl) as with_bbl,
        COUNT(arch_build) as with_architect,
        COUNT(date_combo) as with_date,
        COUNT(style_sec) as with_style_sec,
        COUNT(short_bio) as with_bio
    FROM buildings
""")
stats = cur.fetchone()
print(f'\nMetadata coverage:')
print(f'  Total: {stats[0]}')
print(f'  BBL: {stats[1]}')
print(f'  Architect: {stats[2]}')
print(f'  Date: {stats[3]}')
print(f'  Secondary style: {stats[4]}')
print(f'  Bio: {stats[5]}')

conn.close()
print('\n✅ Full metadata update complete!')