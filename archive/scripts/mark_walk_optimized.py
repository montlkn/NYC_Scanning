#!/usr/bin/env python3
"""Mark walk-optimized buildings"""

import csv
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))

# Read walk-optimized landmarks
walk_optimized = []
with open('data/landmarks_processed.csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        lat = row.get('latitude', '').strip()
        lng = row.get('longitude', '').strip()
        score = row.get('final_score', '').strip()
        if lat and lng:
            walk_optimized.append((float(lat), float(lng), float(score) if score else None))

print(f"📊 Marking {len(walk_optimized)} walk-optimized buildings...")

updated = 0
with engine.connect() as conn:
    for lat, lng, score in walk_optimized:
        result = conn.execute(text("""
            UPDATE buildings_full_merge_scanning
            SET is_walk_optimized = TRUE,
                walk_score = :score
            WHERE ST_DWithin(
                geom::geography,
                ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                50
            )
        """), {'lat': lat, 'lng': lng, 'score': score})
        updated += result.rowcount
        if updated % 100 == 0:
            print(f"  Marked {updated}...")

    conn.commit()

print(f"✅ Marked {updated} walk-optimized buildings")

# Verify
with engine.connect() as conn:
    result = conn.execute(text("""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE is_walk_optimized = TRUE) as walk_optimized
        FROM buildings_full_merge_scanning
    """))
    row = result.fetchone()
    print(f"\nFinal counts:")
    print(f"  Total: {row[0]}")
    print(f"  Walk-optimized: {row[1]}")
