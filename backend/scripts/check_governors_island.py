import sys
import os
from pathlib import Path
import psycopg2

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

from models.config import get_settings

def main():
    try:
        settings = get_settings()
        print(f"Connecting to DB...")
        conn = psycopg2.connect(settings.database_url)
        cur = conn.cursor()
        
        # Governor's Island is typically Manhattan (1), Block 10
        print("\nSearching for Governor's Island (Block 10)...")
        cur.execute("""
            SELECT bbl, bin, address 
            FROM buildings_full_merge_scanning 
            WHERE bbl LIKE '100010%' OR bbl LIKE '1-00010%'
            LIMIT 20;
        """)
        
        rows = cur.fetchall()
        if not rows:
            print("No records found for Block 10.")
        else:
            print(f"Found {len(rows)} records. First 5:")
            for row in rows[:5]:
                print(f"  BBL: {row[0]}, BIN: {row[1]}, Address: {row[2]}")
                
        # Also check how BBLs are formatted in general
        print("\nChecking BBL format sample:")
        cur.execute("SELECT bbl FROM buildings_full_merge_scanning WHERE bbl IS NOT NULL LIMIT 5;")
        for row in cur.fetchall():
            print(f"  {row[0]!r}")

        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()

