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
        
        # Check if table exists
        cur.execute("SELECT to_regclass('public.buildings_full_merge_scanning');")
        exists = cur.fetchone()[0]
        print(f"Table 'buildings_full_merge_scanning' exists: {exists}")
        
        if exists:
            # Count total rows
            cur.execute("SELECT count(*) FROM buildings_full_merge_scanning;")
            total = cur.fetchone()[0]
            print(f"Total rows in table: {total}")
            
            # Count rows with BBL and BIN
            cur.execute("SELECT count(*) FROM buildings_full_merge_scanning WHERE bbl IS NOT NULL AND bin IS NOT NULL;")
            valid = cur.fetchone()[0]
            print(f"Rows with BBL and BIN: {valid}")
            
            # Show sample
            cur.execute("SELECT bbl, bin FROM buildings_full_merge_scanning WHERE bbl IS NOT NULL LIMIT 5;")
            print("Sample data:")
            for row in cur.fetchall():
                print(row)
        else:
            print("Table does not exist. Listing available tables:")
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
            for row in cur.fetchall():
                print(row[0])
                
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
