#!/usr/bin/env python3
"""
Cleanup script to prune folders for buildings where building_name is '0'.
These were incorrectly scraped with generic results and need to be removed.
"""

import os
import sys
import shutil
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

# Add backend directory to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Load environment variables
load_dotenv(dotenv_path=backend_dir / '.env')

from models.config import get_settings

# Local Storage Path (iCloud Drive base)
LOCAL_STORAGE_BASE_DIR = Path("/Users/lucienmount/Library/Mobile Documents/com~apple~CloudDocs/sourced_images")

def main():
    print("🧹 Starting cleanup of '0' name building folders...")
    
    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()
    
    # 1. Get all BINs where building_name is '0'
    print("📊 Querying database for buildings named '0'...")
    cur.execute("""
        SELECT bin 
        FROM buildings_full_merge_scanning 
        WHERE building_name = '0'
    """)
    
    rows = cur.fetchall()
    zero_bins = {str(row[0]).replace('.0', '') for row in rows}
    
    print(f"⚠️ Found {len(zero_bins)} buildings with name '0' in database.")
    
    to_delete = []
    total_size = 0
    
    # 2. Check for folders to delete
    if not LOCAL_STORAGE_BASE_DIR.exists():
        print(f"❌ Storage directory not found: {LOCAL_STORAGE_BASE_DIR}")
        return

    print(f"📂 Scanning {LOCAL_STORAGE_BASE_DIR}...")
    
    for bin_folder in LOCAL_STORAGE_BASE_DIR.iterdir():
        if not bin_folder.is_dir():
            continue
            
        bin_id = bin_folder.name
        
        if bin_id in zero_bins:
            # Calculate size
            folder_size = sum(f.stat().st_size for f in bin_folder.glob('**/*') if f.is_file())
            to_delete.append((bin_folder, folder_size))
            total_size += folder_size

    if not to_delete:
        print("✅ No '0' name folders found locally. Nothing to clean.")
        return

    print("\n" + "="*60)
    print(f"⚠️ FOUND {len(to_delete)} FOLDERS TO DELETE")
    print(f"💾 Total Size: {total_size/1024/1024:.2f} MB")
    print("="*60)
    
    # List first 10 as examples
    print("\nExamples:")
    for folder, size in to_delete[:10]:
        print(f" - {folder.name} ({size/1024:.1f} KB)")
    if len(to_delete) > 10:
        print(f" ... and {len(to_delete) - 10} more.")

    # 3. Ask for confirmation
    confirm = input("\n🔴 Are you sure you want to delete these folders? (yes/no): ").lower().strip()
    
    if confirm != 'yes':
        print("🛑 Operation cancelled.")
        return
        
    print("\n🚀 Deleting...")
    deleted_count = 0
    
    for folder, size in to_delete:
        try:
            shutil.rmtree(folder)
            deleted_count += 1
            if deleted_count % 100 == 0:
                print(f"   Deleted {deleted_count}/{len(to_delete)}...")
        except Exception as e:
            print(f"   ❌ Failed to delete {folder.name}: {e}")

    conn.close()
    
    print("\n" + "="*60)
    print("✅ CLEANUP COMPLETE")
    print(f"🗑️ Total folders deleted: {deleted_count}")
    print("="*60)

if __name__ == "__main__":
    main()
