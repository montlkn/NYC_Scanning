# scripts/manual_fix_issues.py
import pandas as pd
import psycopg2
import os

SCAN_DB_URL = os.getenv('SCAN_DB_URL')

# Load landmarks
df = pd.read_csv('data/Individual_Landmark_and_Historic_District_Building_Database_20250918.csv')

conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

def safe_str(val):
    if pd.isna(val):
        return None
    return str(val)

def safe_int(val):
    if pd.isna(val):
        return None
    return int(val)

# Issues to fix
issues = [
    {
        'id': 117,
        'address': '630 Fifth Avenue',
        'current_bbl': '7953318554',
        'correct_bbl': '1012660001',
        'missing': ['architect', 'date', 'mat_prim']
    },
    {
        'id': 143,
        'address': 'Hospital Road. between Squibb Place and Oman Road',
        'current_bbl': '1014120062',
        'correct_bbl': '3020230150',
        'missing': ['architect', 'date', 'mat_prim']
    },
    {
        'id': 149,
        'address': '122-124 East 66th Street',
        'current_bbl': None,
        'correct_bbl': None,
        'missing': ['architect', 'date', 'mat_prim']
    },
    {
        'id': 183,
        'address': '693-697 Broadway (aka 2-6 West 4th Street)',
        'current_bbl': '1005230048',
        'correct_bbl': '1005357501',
        'missing': ['architect', 'date', 'mat_prim']
    },
    {
        'id': 187,
        'address': '768 Fifth Avenue and 2 Central Park South',
        'current_bbl': '1012747505',
        'correct_bbl': '1012747504',
        'missing': []
    },
    {
        'id': 194,
        'address': '1131-1137 Broadway (aka 10 West 26th Street)',
        'current_bbl': None,
        'correct_bbl': None,
        'missing': ['architect', 'date', 'mat_prim']
    }
]

print("="*60)
print("MANUAL FIX: BBLs AND MISSING METADATA")
print("="*60)

for issue in issues:
    print(f"\n{'='*60}")
    print(f"ID {issue['id']}: {issue['address']}")
    print(f"{'='*60}")
    
    # Show BBL issue if exists
    if issue['correct_bbl'] and issue['current_bbl'] != issue['correct_bbl']:
        print(f"\n⚠️  BBL MISMATCH:")
        print(f"  Current DB: {issue['current_bbl']}")
        print(f"  NYC CSV:    {issue['correct_bbl']}")
        
        fix_bbl = input("\nFix BBL? (y/n): ").strip().lower()
        
        if fix_bbl == 'y':
            # Check if R2 folder exists
            print(f"\n⚠️  NOTE: Changing BBL will require renaming R2 folder:")
            print(f"  From: {issue['current_bbl']}/")
            print(f"  To:   {issue['correct_bbl']}/")
            confirm = input("Confirm BBL update? (yes/no): ").strip().lower()
            
            if confirm == 'yes':
                cur.execute("""
                    UPDATE buildings SET bbl = %s WHERE id = %s
                """, (issue['correct_bbl'], issue['id']))
                conn.commit()
                print(f"✅ BBL updated to {issue['correct_bbl']}")
                print(f"⚠️  TODO: Rename R2 folder manually!")
            else:
                print("⏭️  Skipped BBL update")
    
    # Show missing metadata
    if issue['missing']:
        print(f"\n⚠️  MISSING DATA: {', '.join(issue['missing'])}")
        
        # Find match in CSV
        match = df[df['Des_Addres'].str.lower().str.strip() == issue['address'].lower().strip()]
        
        if len(match) > 0:
            row = match.iloc[0]
            print(f"\nFound in NYC CSV:")
            print(f"  Architect: {row.get('Arch_Build')}")
            print(f"  Date:      {row.get('Date_Combo')}")
            print(f"  Material:  {row.get('Mat_Prim')}")
            print(f"  Style Sec: {row.get('Style_Sec')}")
            
            fix_metadata = input("\nUpdate with this data? (y/n): ").strip().lower()
            
            if fix_metadata == 'y':
                cur.execute("""
                    UPDATE buildings
                    SET 
                        arch_build = COALESCE(arch_build, %s),
                        date_combo = COALESCE(date_combo, %s),
                        date_low = COALESCE(date_low, %s),
                        date_high = COALESCE(date_high, %s),
                        mat_prim = COALESCE(mat_prim, %s),
                        style_sec = COALESCE(style_sec, %s)
                    WHERE id = %s
                """, (
                    safe_str(row.get('Arch_Build')),
                    safe_str(row.get('Date_Combo')),
                    safe_int(row.get('Date_Low')),
                    safe_int(row.get('Date_High')),
                    safe_str(row.get('Mat_Prim')),
                    safe_str(row.get('Style_Sec')),
                    issue['id']
                ))
                conn.commit()
                print("✅ Metadata updated")
            else:
                print("⏭️  Skipped metadata update")
        else:
            print(f"⚠️  Not found in NYC CSV, manual entry required")
    
    print()

conn.close()
print(f"\n{'='*60}")
print("✅ MANUAL FIX COMPLETE")
print(f"{'='*60}")
print("\n⚠️  REMEMBER: If you changed any BBLs, you need to:")
print("  1. Rename R2 folders in Cloudflare")
print("  2. Update reference_embeddings.image_key paths")
print("  3. Or re-cache those buildings")