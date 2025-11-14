# scripts/fuzzy_match_interactive.py
import pandas as pd
import psycopg2
import os
from fuzzywuzzy import fuzz

SCAN_DB_URL = os.getenv('SCAN_DB_URL')

# Load NYC Landmarks dataset
print("Loading NYC Landmarks dataset...")
df = pd.read_csv('data/Individual_Landmark_and_Historic_District_Building_Database_20250918.csv')
print(f'Loaded {len(df)} landmarks')

# Clean addresses and filter out NaN
df['address_clean'] = df['Des_Addres'].astype(str).str.lower().str.strip()
df = df[df['Des_Addres'].notna()].copy()  # Remove rows with no address
print(f'After filtering: {len(df)} landmarks with addresses')

# Unmatched building IDs
unmatched_ids = [112, 116, 140, 117, 143, 149, 183, 194]

conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

def safe_int(val):
    if pd.isna(val):
        return None
    return int(val)

def safe_str(val):
    if pd.isna(val):
        return None
    return str(val)

def update_building(building_id, match_row):
    """Update building with matched metadata"""
    cur.execute("""
        UPDATE buildings SET
            bbl = %s,
            bin = %s,
            borough = %s,
            block = %s,
            lot = %s,
            date_low = %s,
            date_high = %s,
            date_combo = %s,
            alt_date_1 = %s,
            alt_date_2 = %s,
            arch_build = %s,
            own_devel = %s,
            alt_arch_1 = %s,
            alt_arch_2 = %s,
            altered = %s,
            style_sec = %s,
            style_oth = %s,
            mat_prim = %s,
            mat_sec = %s,
            mat_third = %s,
            mat_four = %s,
            mat_other = %s,
            use_orig = %s,
            use_other = %s,
            build_type = %s,
            build_oth = %s,
            notes = %s,
            hist_dist = %s,
            build_nme = %s
        WHERE id = %s
    """, (
        safe_str(match_row['BBL']),
        safe_int(match_row['BIN']),
        safe_str(match_row.get('Borough')),
        safe_int(match_row.get('Block')),
        safe_int(match_row.get('Lot')),
        safe_int(match_row.get('Date_Low')),
        safe_int(match_row.get('Date_High')),
        safe_str(match_row.get('Date_Combo')),
        safe_str(match_row.get('Alt_Date_1')),
        safe_str(match_row.get('Alt_Date_2')),
        safe_str(match_row.get('Arch_Build')),
        safe_str(match_row.get('Own_Devel')),
        safe_str(match_row.get('Alt_Arch_1')),
        safe_str(match_row.get('Alt_Arch_2')),
        safe_int(match_row.get('Altered')),
        safe_str(match_row.get('Style_Sec')),
        safe_str(match_row.get('Style_Oth')),
        safe_str(match_row.get('Mat_Prim')),
        safe_str(match_row.get('Mat_Sec')),
        safe_str(match_row.get('Mat_Third')),
        safe_str(match_row.get('Mat_Four')),
        safe_str(match_row.get('Mat_Other')),
        safe_str(match_row.get('Use_Orig')),
        safe_str(match_row.get('Use_Other')),
        safe_str(match_row.get('Build_Type')),
        safe_str(match_row.get('Build_Oth')),
        safe_str(match_row.get('Notes')),
        safe_str(match_row.get('Hist_Dist')),
        safe_str(match_row.get('Build_Nme')),
        building_id
    ))
    conn.commit()

print("Interactive Fuzzy Matching\n" + "="*60)

matched_count = 0

for building_id in unmatched_ids:
    # Get the address
    cur.execute('SELECT des_addres FROM buildings WHERE id = %s', (building_id,))
    result = cur.fetchone()
    if not result:
        continue
    
    address = result[0]
    address_clean = address.lower().strip()
    
    print(f"\n🏢 ID {building_id}: {address}")
    print("-" * 60)
    
    # Find best fuzzy matches (handle NaN safely)
    df['score'] = df['address_clean'].apply(
        lambda x: fuzz.ratio(address_clean, str(x)) if pd.notna(x) else 0
    )
    
    top_matches = df.nlargest(10, 'score')[['Des_Addres', 'BBL', 'Build_Nme', 'Arch_Build', 'Date_Combo', 'score']].reset_index(drop=True)
    
    print("\nTop matches:")
    for i, row in top_matches.iterrows():
        print(f"  [{i+1}] Score {row['score']:3d} | {row['Des_Addres']}")
        print(f"      Name: {row['Build_Nme']} | BBL: {row['BBL']}")
        print(f"      Architect: {row['Arch_Build']} | Date: {row['Date_Combo']}")
    
    # User selection
    while True:
        choice = input("\nSelect match (1-10), 's' to skip, 'q' to quit: ").strip().lower()
        
        if choice == 'q':
            print("\n❌ Quitting...")
            conn.close()
            exit()
        
        if choice == 's':
            print("⏭️  Skipped")
            break
        
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(top_matches):
                selected = top_matches.iloc[idx]
                match_row = df[df['Des_Addres'] == selected['Des_Addres']].iloc[0]
                
                print(f"✅ Matched to: {selected['Des_Addres']}")
                update_building(building_id, match_row)
                matched_count += 1
                break
            else:
                print("Invalid selection. Try again.")
        except ValueError:
            print("Invalid input. Enter a number, 's', or 'q'.")

conn.close()
print(f"\n{'='*60}")
print(f"✅ Interactive matching complete!")
print(f"   Matched: {matched_count}/8")