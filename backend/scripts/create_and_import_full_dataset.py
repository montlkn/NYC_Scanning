#!/usr/bin/env python3
"""
Create buildings_full_merge_scanning table with all 159 columns from CSV
and import the data
"""

import os
import sys
import csv
from dotenv import load_dotenv

try:
    import psycopg2
except ImportError:
    print("❌ psycopg2 not installed. Install with: pip install psycopg2-binary")
    sys.exit(1)

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ Missing DATABASE_URL in .env")
    sys.exit(1)

try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
except Exception as e:
    print(f"❌ Could not connect to database: {e}")
    sys.exit(1)

def get_columns_from_csv(csv_path):
    """Extract column names from CSV header"""
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return reader.fieldnames

def create_table(cursor, table_name, columns):
    """Create table with all columns as TEXT (simplest approach)"""

    print(f"Creating table {table_name} with {len(columns)} columns...")

    # Drop existing table
    cursor.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
    conn.commit()

    # Build CREATE TABLE statement
    quoted_cols = [f'"{col}" TEXT' for col in columns]
    create_sql = f"CREATE TABLE {table_name} (\n  id SERIAL PRIMARY KEY,\n  " + ",\n  ".join(quoted_cols) + "\n)"

    try:
        cursor.execute(create_sql)
        conn.commit()
        print(f"✅ Table {table_name} created successfully")
    except Exception as e:
        conn.rollback()
        print(f"❌ Failed to create table: {e}")
        sys.exit(1)

def import_data(csv_path, table_name, columns, batch_size=1000):
    """Import CSV data into table"""

    print(f"\n📂 Importing data from {csv_path}...")

    total_rows = 0
    batch = []
    batch_rows = []
    failed_rows = []

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row_num, row in enumerate(reader, start=2):
                try:
                    values = [row.get(col) if row.get(col) not in ['', 'nan', 'None'] else None for col in columns]
                    batch.append(values)
                    batch_rows.append(row_num)
                    total_rows += 1

                    if len(batch) >= batch_size:
                        # Insert batch
                        quoted_cols = [f'"{col}"' for col in columns]
                        placeholders = ','.join(['(' + ','.join(['%s'] * len(columns)) + ')' for _ in batch])
                        sql = f"INSERT INTO {table_name} ({','.join(quoted_cols)}) VALUES {placeholders}"
                        flat_values = [item for sublist in batch for item in sublist]

                        try:
                            cursor.execute(sql, flat_values)
                            conn.commit()
                            print(f"⏳ Inserted rows {batch_rows[0]} to {batch_rows[-1]} ({len(batch)} rows)")
                        except Exception as e:
                            conn.rollback()
                            print(f"   ⚠️  Batch failed: {e}")
                            failed_rows.extend(zip(batch_rows, batch))

                        batch = []
                        batch_rows = []

                except Exception as e:
                    print(f"⚠️  Row {row_num} parse error: {e}")
                    failed_rows.append((row_num, row))

            # Insert remaining rows
            if batch:
                quoted_cols = [f'"{col}"' for col in columns]
                placeholders = ','.join(['(' + ','.join(['%s'] * len(columns)) + ')' for _ in batch])
                sql = f"INSERT INTO {table_name} ({','.join(quoted_cols)}) VALUES {placeholders}"
                flat_values = [item for sublist in batch for item in sublist]

                try:
                    cursor.execute(sql, flat_values)
                    conn.commit()
                    print(f"⏳ Inserted final {len(batch)} rows")
                except Exception as e:
                    conn.rollback()
                    print(f"   ⚠️  Final batch failed: {e}")

        # Summary
        print()
        print("=" * 60)
        print(f"✅ Import complete!")
        print(f"   Total rows processed: {total_rows}")
        print(f"   Failed rows: {len(failed_rows)}")

        # Verify
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

    # Get columns from CSV
    columns = get_columns_from_csv(csv_file)

    # Create table
    create_table(cursor, table_name, columns)

    # Import data
    import_data(csv_file, table_name, columns)
