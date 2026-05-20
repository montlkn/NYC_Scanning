#!/usr/bin/env python3
"""
Deduplicate buildings_full_merge_scanning table by BIN
Keeps the most complete record for each BIN
"""

import psycopg2
from collections import defaultdict
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')

def get_completeness_score(row):
    """Calculate completeness score for a building record"""
    score = 0

    # Prefer named buildings over street addresses
    address = row[3]  # address column
    if address and ('world trade center' in address.lower() or
                    'plaza' in address.lower() or
                    not address[0].isdigit()):
        score += 10

    # Award points for non-null fields
    fields_to_check = row[1:]  # Skip id
    for field in fields_to_check:
        if field is not None and str(field).strip():
            score += 1

    return score

def deduplicate_buildings():
    """Find and remove duplicate BIN entries, keeping the most complete record"""

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print("🔍 Finding duplicate BINs...")

    # Find all BINs that appear more than once
    cur.execute("""
        SELECT REPLACE(bin, '.0', '') as bin_clean, COUNT(*) as count
        FROM buildings_full_merge_scanning
        WHERE bin IS NOT NULL AND bin != ''
        GROUP BY REPLACE(bin, '.0', '')
        HAVING COUNT(*) > 1
        ORDER BY count DESC
    """)

    duplicates = cur.fetchall()
    print(f"Found {len(duplicates)} duplicate BINs")

    total_to_delete = 0

    for bin_clean, count in duplicates:
        print(f"\n📍 BIN {bin_clean}: {count} entries")

        # Get all records for this BIN
        cur.execute("""
            SELECT id, bin, bbl, address, borough, geocoded_lat, geocoded_lng
            FROM buildings_full_merge_scanning
            WHERE REPLACE(bin, '.0', '') = %s
            ORDER BY id
        """, (bin_clean,))

        records = cur.fetchall()

        # Calculate completeness score for each record
        scored_records = []
        for record in records:
            score = get_completeness_score(record)
            scored_records.append((score, record))
            print(f"  ID {record[0]}: {record[3]} (score: {score})")

        # Sort by score (highest first)
        scored_records.sort(reverse=True)

        # Keep the record with highest score
        keep_id = scored_records[0][1][0]
        keep_address = scored_records[0][1][3]

        print(f"  ✅ Keeping ID {keep_id}: {keep_address}")

        # Delete all other records
        for score, record in scored_records[1:]:
            delete_id = record[0]
            delete_address = record[3]
            print(f"  ❌ Deleting ID {delete_id}: {delete_address}")

            cur.execute("DELETE FROM buildings_full_merge_scanning WHERE id = %s", (delete_id,))
            total_to_delete += 1

    print(f"\n📊 Summary:")
    print(f"  Duplicate BINs found: {len(duplicates)}")
    print(f"  Records to delete: {total_to_delete}")

    # Auto-commit changes
    print("\nCommitting changes...")
    conn.commit()
    print("✅ Changes committed")

    cur.close()
    conn.close()

if __name__ == '__main__':
    deduplicate_buildings()
