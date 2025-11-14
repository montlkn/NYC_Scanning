import pandas as pd
import psycopg2
import os
from shapely import wkt
from pyproj import Transformer
import hashlib

SCAN_DB_URL = os.getenv('SCAN_DB_URL')

# Transformer from State Plane NY Long Island (EPSG:2263) to WGS84 (EPSG:4326)
transformer = Transformer.from_crs("EPSG:2263", "EPSG:4326", always_xy=True)

df = pd.read_csv('data/walk_optimized_landmarks.csv')
print(f'Loaded {len(df)} buildings from CSV')

top100 = df.nlargest(100, 'final_score')
print(f'Selected top 100 buildings')
print(f'Score range: {top100["final_score"].min():.2f} to {top100["final_score"].max():.2f}')

conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

print('\nClearing existing data...')
cur.execute('DELETE FROM reference_embeddings')
cur.execute('DELETE FROM buildings')
conn.commit()

print('Importing top 100 buildings...')
imported = 0
skipped = 0

for idx, row in top100.iterrows():
    try:
        # Parse geometry and get centroid
        geom = wkt.loads(row['geometry'])
        center_sp = geom.centroid  # State Plane coordinates
        
        # Transform to lat/lng
        lng, lat = transformer.transform(center_sp.x, center_sp.y)
        
        # Generate BBL from address
        bbl = hashlib.md5(row['des_addres'].encode()).hexdigest()[:10]
        
        cur.execute("""
            INSERT INTO buildings 
            (bbl, des_addres, build_nme, style_prim, num_floors, final_score, 
             geom, center, tier)
            VALUES (%s, %s, %s, %s, %s, %s, 
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    1)
        """, (
            bbl,
            row['des_addres'],
            row['build_nme'],
            row.get('style_prim', None),
            int(row['NumFloors']) if pd.notna(row['NumFloors']) else None,
            float(row['final_score']),
            lng, lat,  # geom
            lng, lat   # center
        ))
        
        conn.commit()
        imported += 1
        
        if imported % 10 == 0:
            print(f'  Imported {imported}')
            
    except Exception as e:
        conn.rollback()
        skipped += 1
        print(f'  Skipped {row["build_nme"]}: {str(e)[:100]}')
        continue

print(f'\nImported {imported} buildings, skipped {skipped}')

if imported > 0:
    cur.execute('SELECT COUNT(*), AVG(final_score) FROM buildings WHERE tier=1')
    count, avg_score = cur.fetchone()
    print(f'\nVerified: {count} buildings, avg score: {avg_score:.2f}')

    cur.execute('SELECT bbl, build_nme, des_addres, final_score FROM buildings ORDER BY final_score DESC LIMIT 5')
    print('\nTop 5 buildings:')
    for row in cur.fetchall():
        print(f'  {row[1]} ({row[2]}): {row[3]:.2f}')

conn.close()
print('\n✅ Import complete!')
