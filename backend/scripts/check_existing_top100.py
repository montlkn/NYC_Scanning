"""
Check which buildings from the new top_100.csv are already in Phase 1 DB
and which need to be imported.
"""

import pandas as pd
import sys
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import text

# Load environment variables from .env
load_dotenv(Path(__file__).parent.parent / ".env")

# Add backend to path
sys.path.append(str(Path(__file__).parent.parent))

from models.scan_db import SessionLocal

def main():
    # Read the new top 100 dataset
    csv_path = Path(__file__).parent.parent / "data" / "final" / "top_100.csv"
    print(f"Reading new dataset from: {csv_path}")
    df = pd.read_csv(csv_path)

    print(f"\n📊 New dataset contains {len(df)} buildings")
    print(f"Columns: {', '.join(df.columns[:10])}... ({len(df.columns)} total)")

    # Get BBLs from new dataset (handle potential NaN values)
    new_bbls = set(df['bbl'].dropna().astype(str).str.replace('.0', '', regex=False))
    print(f"\n🆕 New dataset BBLs: {len(new_bbls)}")

    # Query Phase 1 DB
    print("\n🔍 Checking Phase 1 database...")
    db = SessionLocal()
    try:
        # Check if buildings table exists and get existing BBLs
        result = db.execute(text("SELECT bbl, build_nme, tier, final_score FROM buildings ORDER BY final_score DESC"))
        existing_buildings = result.fetchall()

        if not existing_buildings:
            print("⚠️  Phase 1 DB is empty - all buildings need to be imported")
            print(f"\n📝 Buildings to import: {len(df)}")
            print("\nTop 10 buildings from new dataset:")
            for idx, row in df.head(10).iterrows():
                print(f"  {idx+1}. {row['building_name']} (BBL: {row['bbl']}, Score: {row['final_score']:.2f})")
            return

        existing_bbls = set(str(b[0]) for b in existing_buildings)
        print(f"✅ Found {len(existing_buildings)} buildings in Phase 1 DB")

        # Show sample of existing buildings
        print("\n📋 Sample of existing buildings in Phase 1 DB:")
        for i, (bbl, name, tier, score) in enumerate(existing_buildings[:5]):
            print(f"  {i+1}. {name} (BBL: {bbl}, Tier: {tier}, Score: {score})")

        # Compare
        already_exist = new_bbls & existing_bbls
        need_import = new_bbls - existing_bbls
        in_db_not_in_new = existing_bbls - new_bbls

        print(f"\n📊 Comparison Results:")
        print(f"  ✅ Already in Phase 1 DB: {len(already_exist)}")
        print(f"  ➕ Need to import: {len(need_import)}")
        print(f"  ⚠️  In DB but not in new top 100: {len(in_db_not_in_new)}")

        if already_exist:
            print("\n✅ Buildings already in Phase 1 DB:")
            for bbl in sorted(already_exist)[:10]:
                row = df[df['bbl'].astype(str).str.replace('.0', '', regex=False) == bbl].iloc[0]
                print(f"  - {row['building_name']} (BBL: {bbl})")
            if len(already_exist) > 10:
                print(f"  ... and {len(already_exist) - 10} more")

        if need_import:
            print("\n➕ Buildings that need to be imported:")
            need_import_df = df[df['bbl'].astype(str).str.replace('.0', '', regex=False).isin(need_import)]
            need_import_df = need_import_df.sort_values('final_score', ascending=False)
            for idx, row in need_import_df.head(10).iterrows():
                print(f"  - {row['building_name']} (BBL: {row['bbl']}, Score: {row['final_score']:.2f})")
            if len(need_import) > 10:
                print(f"  ... and {len(need_import) - 10} more")

        if in_db_not_in_new:
            print("\n⚠️  Buildings in DB but not in new top 100 (will keep them):")
            for bbl in sorted(in_db_not_in_new)[:5]:
                building = [b for b in existing_buildings if str(b[0]) == bbl][0]
                print(f"  - {building[1]} (BBL: {bbl}, Tier: {building[2]}, Score: {building[3]})")
            if len(in_db_not_in_new) > 5:
                print(f"  ... and {len(in_db_not_in_new) - 5} more")

        print("\n" + "="*80)
        print(f"✅ Ready to import {len(need_import)} new buildings from the dataset")
        print("="*80)

    except Exception as e:
        print(f"❌ Error querying Phase 1 DB: {e}")
        print("\nThis might mean:")
        print("  1. The Phase 1 DB is not accessible (check SCAN_DB_URL env var)")
        print("  2. The buildings table doesn't exist yet (run migration 003)")
        print("  3. Connection credentials are incorrect")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    main()
