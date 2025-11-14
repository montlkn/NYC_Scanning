import pandas as pd
import psycopg2
import os
from difflib import SequenceMatcher

SCAN_DB_URL = os.getenv('SCAN_DB_URL')

df = pd.read_csv('data/bbl_mappings.csv')

conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

unmatched = [
    (112, '695 Park Avenue', 'Hunter College'),
    (116, '1260 Avenue of the Americas', 'Radio City Music Hall'),
    (117, '630 Fifth Avenue', 'International Building'),
    (138, 'Roosevelt Island, located approximately opposite East 52nd Street', 'Chapel of the Good Shepherd'),
    (140, 'Hospital Road. between Squibb Place and Oman Road', 'Smallpox Hospital'),
    (143, '122-124 East 66th Street', 'United States Naval Hospital'),
    (147, '24 West 55th Street', 'University Club'),
    (168, '693-697 Broadway (aka 2-6 West 4th Street)', 'Cable Building'),
    (181, '1131-1137 Broadway (aka 10 West 26th Street)', 'Baudouine Building')
]

def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

print('Fuzzy matching unmatched buildings...')
print()

updated = 0
skipped = 0

for building_id, address, name in unmatched:
    df['name_score'] = df['Build_Nme'].fillna('').apply(lambda x: similarity(x, name))
    best_name = df.nlargest(1, 'name_score').iloc[0]
    
    df['addr_score'] = df['Des_Addres'].fillna('').apply(lambda x: similarity(x, address))
    best_addr = df.nlargest(1, 'addr_score').iloc[0]
    
    if best_name['name_score'] > best_addr['addr_score'] and best_name['name_score'] > 0.6:
        match = best_name
        match_type = 'name'
        score = best_name['name_score']
    elif best_addr['addr_score'] > 0.6:
        match = best_addr
        match_type = 'address'
        score = best_addr['addr_score']
    else:
        print(f'❌ No match: {name}')
        skipped += 1
        continue
    
    real_bbl = str(int(match['BBL']))
    
    print(f'{name}')
    print(f'  Matched by {match_type} (score: {score:.2f})')
    print(f'  → {match["Build_Nme"]} at {match["Des_Addres"]}')
    print(f'  BBL: {real_bbl}')
    
    try:
        cur.execute('UPDATE buildings SET bbl = %s WHERE id = %s', (real_bbl, building_id))
        conn.commit()
        print(f'  ✅ Updated')
        updated += 1
    except Exception as e:
        conn.rollback()
        print(f'  ⚠️  Skipped: {str(e)[:80]}')
        skipped += 1
    
    print()

print(f'Updated: {updated}, Skipped: {skipped}')
conn.close()
