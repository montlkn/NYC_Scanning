# scripts/preview_tier2.py
import pandas as pd

# Load the walk-optimized CSV
df = pd.read_csv('data/walk_optimized_landmarks.csv')

# Get buildings 101-500 (Tier 2)
tier2 = df.iloc[100:500]

print("="*80)
print("TIER 2 PREVIEW: Buildings 101-500 (400 buildings)")
print("="*80)

print(f"\nTotal buildings: {len(tier2)}")
print(f"Score range: {tier2['final_score'].min():.2f} - {tier2['final_score'].max():.2f}")

print("\n" + "="*80)
print("BUILDING LIST")
print("="*80)

for idx, row in tier2.iterrows():
    building_num = idx + 1  # 1-indexed position
    address = row['des_addres']
    name = row.get('build_nme', 'N/A')
    score = row['final_score']
    
    # Show name if it exists and is different from address
    if pd.notna(name) and name.strip() and name.lower() not in address.lower():
        print(f"{building_num:3d}. {address}")
        print(f"     └─ {name} (score: {score:.2f})")
    else:
        print(f"{building_num:3d}. {address} (score: {score:.2f})")

print("\n" + "="*80)
print("SUMMARY STATISTICS")
print("="*80)

# Check BBL coverage
if 'bbl' in tier2.columns:
    bbl_count = tier2['bbl'].notna().sum()
    print(f"Buildings with BBL: {bbl_count}/{len(tier2)} ({bbl_count/len(tier2)*100:.1f}%)")

# Check name coverage
if 'build_nme' in tier2.columns:
    name_count = tier2['build_nme'].notna().sum()
    print(f"Buildings with names: {name_count}/{len(tier2)} ({name_count/len(tier2)*100:.1f}%)")

# Borough breakdown
if 'Borough' in tier2.columns or 'borough' in tier2.columns:
    borough_col = 'Borough' if 'Borough' in tier2.columns else 'borough'
    print(f"\nBorough breakdown:")
    print(tier2[borough_col].value_counts())

print("\n" + "="*80)