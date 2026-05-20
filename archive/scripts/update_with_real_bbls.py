import pandas as pd
import psycopg2
import os

SCAN_DB_URL = os.getenv('SCAN_DB_URL')

# Load the BBL mappings
print('Loading BBL mappings...')
df = pd.read_csv('data/bbl_mappings.csv')
print(f'Loaded {len(df)} buildings with BBLs')

# Clean addresses for matching
df['address_clean'] = df['Des_Addres'].str.lower().str.strip()

conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

# Get current buildings
cur.execute('SELECT id, bbl, des_addres FROM buildings')
current = cur.fetchall()
print(f'Found {len(current)} buildings in scan database')

matched = 0
not_matched = []

for building_id, old_bbl, address in current:
    address_clean = address.lower().strip()
    
    # Find match in BBL mappings
    match = df[df['address_clean'] == address_clean]
    
    if len(match) > 0:
        real_bbl = str(int(match.iloc[0]['BBL']))
        
        # Update with real BBL
        cur.execute('UPDATE buildings SET bbl = %s WHERE id = %s', (real_bbl, building_id))
        matched += 1
    else:
        not_matched.append((building_id, address))

conn.commit()
print(f'\nMatched: {matched}/{len(current)}')
print(f'Not matched: {len(not_matched)}')

if not_matched and len(not_matched) <= 10:
    print('\nBuildings without BBL match:')
    for bid, addr in not_matched[:10]:
        print(f'  {addr}')

# Verify
cur.execute('SELECT COUNT(*), COUNT(DISTINCT bbl) FROM buildings')
total, unique_bbls = cur.fetchone()
print(f'\nVerification:')
print(f'  Total buildings: {total}')
print(f'  Unique BBLs: {unique_bbls}')

# Show sample
cur.execute('SELECT id, bbl, build_nme, des_addres FROM buildings LIMIT 5')
print('\nSample updated buildings:')
for row in cur.fetchall():
    print(f'  ID {row[0]}: BBL {row[1]} - {row[2]}')

conn.close()
print('\n✅ Update complete!')
