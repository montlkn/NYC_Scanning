#!/usr/bin/env python3
"""
Import full_dataset_fixed_bins.csv directly into Supabase

Handles all 159 columns from the full dataset with proper type conversion.
Uses direct PostgreSQL connection for reliability.
"""

import os
import sys
import csv
import json
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import psycopg2
try:
    import psycopg2
    from psycopg2.extras import execute_batch
except ImportError:
    print("❌ psycopg2 not installed. Install with: pip install psycopg2-binary")
    sys.exit(1)

# Get database connection string from environment
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ Missing DATABASE_URL in .env")
    sys.exit(1)

# Parse connection string
try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
except Exception as e:
    print(f"❌ Could not connect to database: {e}")
    sys.exit(1)

def parse_value(value):
    """
    Convert CSV string values to appropriate Python types
    """
    if value is None or value == '' or value == 'nan':
        return None

    value = str(value).strip()

    if value.lower() in ['nan', 'none', '']:
        return None

    if value.lower() in ['true', '1', 'yes']:
        return True
    if value.lower() in ['false', '0', 'no']:
        return False

    # Try to parse as number
    try:
        if '.' in value:
            return float(value)
        else:
            return int(value)
    except ValueError:
        pass

    # Return as string
    return value

def import_csv(csv_path, table_name, batch_size=1000):
    """
    Import CSV file to PostgreSQL table in batches
    """
    global cursor, conn

    if not os.path.exists(csv_path):
        print(f"❌ File not found: {csv_path}")
        sys.exit(1)

    print(f"📂 Reading {csv_path}...")
    print(f"📊 Target table: {table_name}")

    total_rows = 0
    batch = []
    batch_rows = []
    failed_rows = []

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            if not reader.fieldnames:
                print("❌ CSV has no headers")
                sys.exit(1)

            columns = reader.fieldnames
            print(f"✓ Found {len(columns)} columns")
            print(f"  Columns: {', '.join(columns[:10])}...")
            print()

            for row_num, row in enumerate(reader, start=2):  # Start at 2 (row 1 is header)
                try:
                    # Convert values using parse_value
                    values = []
                    for col in columns:
                        values.append(parse_value(row.get(col)))

                    batch.append(values)
                    batch_rows.append(row_num)
                    total_rows += 1

                    # Insert in batches
                    if len(batch) >= batch_size:
                        print(f"⏳ Inserting rows {batch_rows[0]} to {batch_rows[-1]}...")
                        try:
                            # Quote column names to handle dots and special characters
                            quoted_cols = [f'"{col}"' for col in columns]
                            placeholders = ','.join(['(' + ','.join(['%s'] * len(columns)) + ')' for _ in batch])
                            sql = f"INSERT INTO {table_name} ({','.join(quoted_cols)}) VALUES {placeholders}"
                            flat_values = [item for sublist in batch for item in sublist]
                            cursor.execute(sql, flat_values)
                            conn.commit()
                            print(f"   ✓ Inserted {len(batch)} rows")
                        except Exception as e:
                            conn.rollback()
                            print(f"   ❌ Batch insert failed: {e}")
                            failed_rows.extend(zip(batch_rows, [row for row in batch]))

                        batch = []
                        batch_rows = []

                except Exception as e:
                    print(f"⚠️  Row {row_num} parse error: {e}")
                    failed_rows.append((row_num, row))

            # Insert remaining rows
            if batch:
                print(f"⏳ Inserting final {len(batch)} rows...")
                try:
                    # Quote column names to handle dots and special characters
                    quoted_cols = [f'"{col}"' for col in columns]
                    placeholders = ','.join(['(' + ','.join(['%s'] * len(columns)) + ')' for _ in batch])
                    sql = f"INSERT INTO {table_name} ({','.join(quoted_cols)}) VALUES {placeholders}"
                    flat_values = [item for sublist in batch for item in sublist]
                    cursor.execute(sql, flat_values)
                    conn.commit()
                    print(f"   ✓ Inserted {len(batch)} rows")
                except Exception as e:
                    conn.rollback()
                    print(f"   ❌ Final batch insert failed: {e}")
                    failed_rows.extend(zip(batch_rows, [row for row in batch]))

        # Summary
        print()
        print("=" * 60)
        print(f"✅ Import complete!")
        print(f"   Total rows processed: {total_rows}")
        print(f"   Failed rows: {len(failed_rows)}")

        if failed_rows:
            print()
            print("Failed rows (first 5):")
            for row_num, row_data in failed_rows[:5]:
                if isinstance(row_data, dict):
                    print(f"   Row {row_num}: {row_data.get('building_name', 'N/A')} - {row_data.get('address', 'N/A')}")
                else:
                    print(f"   Row {row_num}: {row_data}")

        # Verify count
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            final_count = cursor.fetchone()[0]
            print()
            print(f"🔍 Database verification:")
            print(f"   Rows in {table_name}: {final_count}")
        except Exception as e:
            print(f"⚠️  Could not verify row count: {e}")

    except Exception as e:
        print(f"❌ Import failed: {e}")
        sys.exit(1)
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    csv_file = 'data/final/full_dataset_fixed_bins.csv'
    table_name = 'buildings_full_merge_scanning'

    import_csv(csv_file, table_name)
