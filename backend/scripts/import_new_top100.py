"""
Import new buildings from final dataset (top_100.csv) into Phase 1 DB.
Additive import - only adds new buildings, preserves existing data.
"""

import pandas as pd
import sys
import os
from pathlib import Path
from sqlalchemy import text
from shapely import wkt
from shapely.geometry import Point

# Add backend to path
sys.path.append(str(Path(__file__).parent.parent))

from models.scan_db import SessionLocal, engine
from sqlalchemy.pool import NullPool

# Recreate engine with NullPool for Supabase
engine = engine.execution_options(isolation_level="AUTOCOMMIT").engine
engine.pool = NullPool(engine.pool._creator)

def parse_geometry(geom_wkt):
    """Parse WKT geometry and return centroid as Point"""
    try:
        geom = wkt.loads(geom_wkt)
        centroid = geom.centroid
        return centroid
    except Exception as e:
        print(f"⚠️  Error parsing geometry: {e}")
        return None

def main():
    # Read the new top 100 dataset
    csv_path = Path(__file__).parent.parent / "data" / "final" / "top_100.csv"
    print(f"📖 Reading new dataset from: {csv_path}")
    df = pd.read_csv(csv_path)

    print(f"\n📊 New dataset contains {len(df)} buildings")

    # Get BBLs from new dataset (handle potential NaN values and .0 suffix)
    new_bbls = set(df['bbl'].dropna().astype(str).str.replace('.0', '', regex=False))
    print(f"🆕 New dataset BBLs: {len(new_bbls)}")

    # Query Phase 1 DB for existing BBLs
    print("\n🔍 Checking Phase 1 database for existing buildings...")
    db = SessionLocal()

    try:
        result = db.execute(text("SELECT bbl FROM buildings"))
        existing_bbls = set(str(row[0]) for row in result.fetchall())
        print(f"✅ Found {len(existing_bbls)} existing buildings in Phase 1 DB")

        # Determine which buildings to import
        to_import = new_bbls - existing_bbls
        already_exist = new_bbls & existing_bbls

        print(f"\n📊 Import Analysis:")
        print(f"  ✅ Already in DB: {len(already_exist)}")
        print(f"  ➕ Need to import: {len(to_import)}")

        if not to_import:
            print("\n✨ All buildings from new dataset already exist in Phase 1 DB!")
            print("Nothing to import.")
            return

        # Filter dataframe to only buildings that need importing
        import_df = df[df['bbl'].astype(str).str.replace('.0', '', regex=False).isin(to_import)]
        import_df = import_df.sort_values('final_score', ascending=False)

        print(f"\n📝 Buildings to import:")
        for idx, row in import_df.head(10).iterrows():
            print(f"  - {row['building_name']} (BBL: {row['bbl']}, Score: {row['final_score']:.2f})")
        if len(to_import) > 10:
            print(f"  ... and {len(to_import) - 10} more")

        # Auto-confirm for non-interactive mode
        import_arg = sys.argv[1] if len(sys.argv) > 1 else None
        if import_arg != '--yes':
            try:
                response = input(f"\n❓ Import {len(to_import)} buildings? (y/n): ")
                if response.lower() != 'y':
                    print("❌ Import cancelled.")
                    return
            except EOFError:
                print("\n⚠️  Non-interactive mode detected. Use --yes flag to auto-confirm.")
                print("❌ Import cancelled.")
                return
        else:
            print(f"\n✅ Auto-confirming import of {len(to_import)} buildings (--yes flag)")

        # Import buildings
        print(f"\n🚀 Starting import of {len(to_import)} buildings...")
        imported = 0
        errors = 0

        for idx, row in import_df.iterrows():
            try:
                # Parse BBL
                bbl = str(row['bbl']).replace('.0', '')

                # Parse geometry - Phase 1 DB expects POINT, not MULTIPOLYGON
                # Use centroid for both geom and center
                if pd.notna(row.get('geometry')):
                    centroid = parse_geometry(row['geometry'])
                    if centroid:
                        point_wkt = f'POINT({centroid.x} {centroid.y})'
                    else:
                        # Fallback to lat/lng if geometry parsing fails
                        point_wkt = f'POINT({row["geocoded_lng"]} {row["geocoded_lat"]})'
                else:
                    # Use geocoded lat/lng
                    point_wkt = f'POINT({row["geocoded_lng"]} {row["geocoded_lat"]})'

                # Prepare data
                building_name = row.get('building_name', '')
                address = row.get('address', '')
                style_prim = row.get('style_prim', row.get('style', ''))

                # Handle num_floors (try multiple columns)
                num_floors = None
                for col in ['num_floors', 'NumFloors']:
                    if col in row and pd.notna(row[col]):
                        num_floors = int(row[col])
                        break

                final_score = float(row.get('final_score', 0))

                # Insert into database
                insert_sql = text("""
                    INSERT INTO buildings (bbl, des_addres, build_nme, style_prim, num_floors, final_score, geom, center, tier)
                    VALUES (
                        :bbl,
                        :des_addres,
                        :build_nme,
                        :style_prim,
                        :num_floors,
                        :final_score,
                        ST_GeomFromText(:geom, 4326),
                        ST_GeomFromText(:center, 4326),
                        1
                    )
                """)

                db.execute(insert_sql, {
                    'bbl': bbl,
                    'des_addres': address,
                    'build_nme': building_name,
                    'style_prim': style_prim,
                    'num_floors': num_floors,
                    'final_score': final_score,
                    'geom': point_wkt,
                    'center': point_wkt
                })

                db.commit()
                imported += 1
                print(f"  ✅ [{imported}/{len(to_import)}] Imported: {building_name} (BBL: {bbl})")

            except Exception as e:
                errors += 1
                print(f"  ❌ Error importing {row.get('building_name', 'Unknown')} (BBL: {row['bbl']}): {e}")
                db.rollback()
                continue

        print(f"\n✨ Import complete!")
        print(f"  ✅ Successfully imported: {imported}")
        print(f"  ❌ Errors: {errors}")

        # Verify total count
        result = db.execute(text("SELECT COUNT(*) FROM buildings WHERE tier = 1"))
        total = result.scalar()
        print(f"\n📊 Phase 1 DB now contains {total} tier 1 buildings")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    main()
