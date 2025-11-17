#!/usr/bin/env python3
"""
Fetch missing BINs from NYC BUILDING_20251104.csv

This script uses the official NYC Building data (BIN -> BASE_BBL mapping)
to automatically fill in missing BINs for our dataset.

Usage: python3 scripts/fetch_bins_from_building_data.py
"""

import csv
from pathlib import Path
from typing import Dict

def load_building_bin_mapping(building_csv: Path) -> Dict[str, str]:
    """Load BIN mapping from NYC Building data"""

    mapping = {}

    with open(building_csv, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            bbl_raw = str(row.get('BASE_BBL', '')).strip()
            bin_val = str(row.get('BIN', '')).strip()

            if bbl_raw and bin_val:
                # Normalize BBL: convert "4075320028" to "4075320028" (10 digits)
                # Our CSV has it as "4075320028.0" so strip .0 if present
                bbl = bbl_raw.split('.')[0] if '.' in bbl_raw else bbl_raw

                if len(bbl) == 10 and len(bin_val) > 0:
                    # Handle multiple BINs per BBL (complex lots)
                    if bbl not in mapping:
                        mapping[bbl] = bin_val
                    else:
                        # Store as comma-separated if multiple
                        existing = mapping[bbl]
                        if bin_val not in existing.split(','):
                            mapping[bbl] = f"{existing},{bin_val}"

    return mapping

def main():
    """Main execution"""

    # Paths
    building_csv = Path('data/BUILDING_20251104.csv')
    research_csv = Path('data/final/bin_research_results.csv')
    output_csv = Path('data/final/bin_research_results_with_building_data.csv')

    if not building_csv.exists():
        print(f"❌ Building CSV not found: {building_csv}")
        return 1

    if not research_csv.exists():
        print(f"❌ Research CSV not found: {research_csv}")
        return 1

    print("\n" + "="*70)
    print("FETCHING BINs FROM NYC BUILDING DATA")
    print("="*70)
    print(f"Building Data: {building_csv}")
    print(f"Research File: {research_csv}")
    print(f"Output:        {output_csv}\n")

    # Load BIN mapping from official NYC data
    print("Loading BIN mapping from NYC Building data...")
    bin_mapping = load_building_bin_mapping(building_csv)
    print(f"✅ Loaded {len(bin_mapping):,} BBL→BIN mappings\n")

    # Read research results and fill in BINs
    print("Filling in missing BINs...")
    rows = []
    stats = {
        'total': 0,
        'already_have_bin': 0,
        'found_via_building_data': 0,
        'still_missing': 0
    }

    with open(research_csv, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats['total'] += 1
            bbl_raw = str(row.get('bbl', '')).strip()
            # Normalize BBL: remove .0 if present
            bbl = bbl_raw.split('.')[0] if '.' in bbl_raw else bbl_raw
            current_bin = str(row.get('real_bin', '')).strip()

            # If already has a BIN, keep it
            if current_bin and current_bin != '':
                stats['already_have_bin'] += 1
                rows.append(row)
                continue

            # Try to find BIN from building data
            if bbl in bin_mapping:
                new_bin = bin_mapping[bbl]
                row['real_bin'] = new_bin
                row['notes'] = f"Found via NYC Building data: {new_bin}"
                stats['found_via_building_data'] += 1
                print(f"✅ {bbl}: {row['building_name'][:40]:40} → BIN {new_bin}")
            else:
                stats['still_missing'] += 1
                row['notes'] = "Check NYC BIS Web for actual BIN"
                print(f"⚠️  {bbl}: {row['building_name'][:40]:40} → NOT FOUND")

            rows.append(row)

    # Write output
    print(f"\nWriting results...")
    with open(output_csv, 'w', newline='') as f:
        fieldnames = rows[0].keys() if rows else []
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # Print summary
    print("\n" + "="*70)
    print("RESULTS SUMMARY")
    print("="*70)
    print(f"Total buildings:            {stats['total']}")
    print(f"Already had BINs:           {stats['already_have_bin']}")
    print(f"Found via Building data:    {stats['found_via_building_data']}")
    print(f"Still missing:              {stats['still_missing']}")
    print("="*70 + "\n")

    if stats['found_via_building_data'] > 0:
        print(f"✅ Success! Found {stats['found_via_building_data']} additional BINs")
        print(f"\nResults saved to: {output_csv}")
        print("\nNext steps:")
        print("1. Review the updated file:")
        print(f"   open {output_csv}")
        print("2. For any remaining REQUIRES_MANUAL_RESEARCH buildings:")
        print("   - Visit: https://a810-bisweb.nyc.gov/bisweb/PropertyProfileHelp.html")
        print("   - Enter BBL and find the BIN")
        print("3. Run: python3 scripts/apply_bin_research.py")
        print("4. Run: ./scripts/load_clean_data.sh\n")
    else:
        print("⚠️  No additional BINs found. Proceed with manual research.")

    return 0

if __name__ == '__main__':
    exit(main())
