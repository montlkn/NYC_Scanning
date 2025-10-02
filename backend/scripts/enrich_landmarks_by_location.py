#!/usr/bin/env python3
"""
Enrich landmarks by matching lat/lng to nearest building

Since landmarks CSV has no BBL, we match by location
"""

import csv
import os
import sys
from pathlib import Path
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

def enrich_landmarks(csv_path):
    """Match landmarks to buildings by lat/lng proximity"""

    # Read landmarks
    landmarks = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            lat = row.get('latitude', '').strip()
            lng = row.get('longitude', '').strip()
            if lat and lng:
                landmarks.append({
                    'address': row.get('des_addres', '').strip(),
                    'name': row.get('build_nme', '').strip(),
                    'architect': row.get('arch_build', '').strip(),
                    'style': row.get('style_prim', '').strip(),
                    'year': row.get('build_year', '').strip(),
                    'score': row.get('final_score', '').strip(),
                    'lat': float(lat),
                    'lng': float(lng)
                })

    print(f"📊 Found {len(landmarks)} landmarks with coordinates")

    # Update buildings
    engine = create_engine(DATABASE_URL)
    updated = 0
    not_found = 0

    with engine.connect() as conn:
        for lm in landmarks:
            # Find nearest building within 50 meters
            result = conn.execute(text("""
                SELECT bbl FROM buildings_full_merge_scanning
                WHERE ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                    50
                )
                ORDER BY ST_Distance(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
                )
                LIMIT 1
            """), {'lat': lm['lat'], 'lng': lm['lng']})

            row = result.fetchone()

            if row:
                # Update building - convert types before passing to SQL
                try:
                    year_int = int(float(lm['year'])) if lm['year'] else None
                except:
                    year_int = None

                try:
                    score_float = float(lm['score']) if lm['score'] else None
                except:
                    score_float = None

                conn.execute(text("""
                    UPDATE buildings_full_merge_scanning
                    SET
                        is_landmark = TRUE,
                        landmark_name = :name,
                        architect = :architect,
                        architectural_style = :style,
                        year_built = COALESCE(:year, year_built),
                        final_score = COALESCE(:score, final_score)
                    WHERE bbl = :bbl
                """), {
                    'bbl': row[0],
                    'name': lm['name'] or None,
                    'architect': lm['architect'] or None,
                    'style': lm['style'] or None,
                    'year': year_int,
                    'score': score_float
                })
                updated += 1
                if updated % 100 == 0:
                    print(f"  Updated {updated}/{len(landmarks)}...")
            else:
                not_found += 1

        conn.commit()

    print(f"\n✅ Complete!")
    print(f"   Updated: {updated}")
    print(f"   Not found: {not_found}")

if __name__ == '__main__':
    enrich_landmarks('data/landmarks_processed.csv')
