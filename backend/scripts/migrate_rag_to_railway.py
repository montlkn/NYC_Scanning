#!/usr/bin/env python3
"""
Migrate landmark_chunks from Supabase to Railway
"""

import psycopg2
from psycopg2.extras import execute_batch

SUPABASE_URL = "postgresql://postgres:nopxug-jywRi0-petnyr@db.cglsuoymdcchrxyzofjb.supabase.co:5432/postgres"
RAILWAY_URL = "postgres://postgres:FgefB6c14fGCGbG4EdEb2a3D2F4b4cEB@metro.proxy.rlwy.net:56050/railway"

def main():
    print("Connecting to Supabase...")
    src = psycopg2.connect(SUPABASE_URL)

    print("Connecting to Railway...")
    dst = psycopg2.connect(RAILWAY_URL)

    # Create table on Railway (no vector extension needed - using text search)
    print("Creating table on Railway...")
    dst_cur = dst.cursor()
    dst_cur.execute("""
        DROP TABLE IF EXISTS landmark_chunks;
        CREATE TABLE landmark_chunks (
            id SERIAL PRIMARY KEY,
            building_name TEXT,
            bin TEXT,
            bbl TEXT,
            address TEXT,
            chunk_text TEXT NOT NULL,
            chunk_index INT,
            source_file TEXT,
            page_number INT,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    dst.commit()
    print("✅ Table created (text-based search, no vectors)")

    # Get count from Supabase
    src_cur = src.cursor()
    src_cur.execute("SELECT COUNT(*) FROM landmark_chunks")
    total = src_cur.fetchone()[0]
    print(f"📊 Migrating {total} chunks...")

    # Fetch and insert in batches
    BATCH_SIZE = 500
    offset = 0
    migrated = 0

    while offset < total:
        src_cur.execute("""
            SELECT building_name, bin, bbl, address, chunk_text,
                   chunk_index, source_file, page_number
            FROM landmark_chunks
            ORDER BY id
            LIMIT %s OFFSET %s
        """, (BATCH_SIZE, offset))

        rows = src_cur.fetchall()
        if not rows:
            break

        execute_batch(
            dst_cur,
            """
            INSERT INTO landmark_chunks
            (building_name, bin, bbl, address, chunk_text, chunk_index, source_file, page_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows
        )
        dst.commit()

        migrated += len(rows)
        print(f"  Migrated {migrated}/{total} chunks...")
        offset += BATCH_SIZE

    # Create indexes
    print("Creating indexes...")
    dst_cur.execute("CREATE INDEX idx_landmark_chunks_building ON landmark_chunks(building_name);")
    dst_cur.execute("CREATE INDEX idx_landmark_chunks_source ON landmark_chunks(source_file);")
    dst.commit()

    # Verify
    dst_cur.execute("SELECT COUNT(*) FROM landmark_chunks")
    final_count = dst_cur.fetchone()[0]

    print(f"\n✅ Migration complete!")
    print(f"📊 Railway now has {final_count} chunks")

    src.close()
    dst.close()

    print("\n⚠️  Next steps:")
    print("1. Update app to use Railway for RAG queries")
    print("2. Delete landmark_chunks from Supabase to free space")

if __name__ == "__main__":
    main()
