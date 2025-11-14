import pandas as pd
import psycopg2
import os

SCAN_DB_URL = os.getenv('SCAN_DB_URL')
conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

df = pd.read_csv('data/top100.csv')

print(f'Importing {len(df)} buildings...')

for idx, row in df.iterrows():
    cur.execute("""
        INSERT INTO buildings 
        (bbl, des_addres, build_nme, style_prim, num_floors, final_score, 
         geom, center, tier)
        VALUES (%s, %s, %s, %s, %s, %s, 
                ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                1)
    """, (
        row['bbl'], 
        row['des_addres'], 
        row['build_nme'], 
        row['style_prim'], 
        int(row['num_floors']) if pd.notna(row['num_floors']) else None,
        float(row['final_score']),
        float(row['longitude']), 
        float(row['latitude']),
        float(row['longitude']), 
        float(row['latitude'])
    ))
    
    if (idx + 1) % 10 == 0:
        print(f'  Imported {idx + 1}/{len(df)}')

conn.commit()
print(f'✅ Imported {len(df)} buildings')

# Verify
cur.execute("SELECT COUNT(*), AVG(final_score) FROM buildings WHERE tier=1")
count, avg_score = cur.fetchone()
print(f'✅ Verified: {count} buildings, avg score: {avg_score:.2f}')

conn.close()
