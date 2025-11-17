#!/usr/bin/env python3
"""
Apply researched BINs back to the main dataset

This script:
1. Reads bin_research_results.csv (with manually researched BINs filled in)
2. Updates full_dataset_fixed_bins.csv with the real BINs
3. Generates a summary report

Usage: python3 scripts/apply_bin_research.py
"""

import csv
from pathlib import Path
from typing import Dict, List

def read_research_results(research_file: Path) -> Dict[str, str]:
    """Read researched BINs from bin_research_results.csv"""

    bins_found = {}

    with open(research_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            bbl = str(row.get('bbl', '')).strip()
            real_bin = str(row.get('real_bin', '')).strip()

            # Only include rows that have a researched BIN value
            if bbl and real_bin:
                bins_found[bbl] = real_bin

    return bins_found


def update_dataset(
    main_file: Path,
    research_results: Dict[str, str],
    output_file: Path
) -> Dict:
    """Update main dataset with researched BINs"""

    stats = {
        'total_rows': 0,
        'updated': 0,
        'skipped': 0,
        'no_research': 0
    }

    rows = []

    with open(main_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats['total_rows'] += 1
            bbl = str(row.get('bbl', '')).strip()
            current_bin = str(row.get('bin', '')).strip()

            # Check if this building was researched
            if bbl in research_results:
                new_bin = research_results[bbl]
                row['bin'] = new_bin

                if new_bin != current_bin:
                    stats['updated'] += 1
                    print(f"✅ Updated {bbl}: {current_bin} → {new_bin}")
                else:
                    stats['no_research'] += 1
            else:
                stats['skipped'] += 1

            rows.append(row)

    # Write updated dataset
    if rows:
        fieldnames = rows[0].keys()
        with open(output_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return stats


def main():
    """Main execution"""

    # Paths - use the version with building data BINs if available
    research_file = Path('data/final/bin_research_results_with_building_data.csv')
    if not research_file.exists():
        research_file = Path('data/final/bin_research_results.csv')

    main_file = Path('data/final/full_dataset_fixed_bins.csv')
    output_file = Path('data/final/full_dataset_fixed_bins_updated.csv')

    if not research_file.exists():
        print(f"❌ Research file not found: {research_file}")
        return 1

    if not main_file.exists():
        print(f"❌ Main dataset not found: {main_file}")
        return 1

    print("\n" + "="*70)
    print("APPLYING RESEARCHED BINs TO DATASET")
    print("="*70)
    print(f"Research Results: {research_file}")
    print(f"Input Dataset:   {main_file}")
    print(f"Output:          {output_file}\n")

    # Read research results
    print("Reading researched BINs...")
    research_results = read_research_results(research_file)
    print(f"Found {len(research_results)} researched BINs\n")

    # Update dataset
    print("Updating dataset...")
    stats = update_dataset(main_file, research_results, output_file)

    # Print summary
    print("\n" + "="*70)
    print("UPDATE SUMMARY")
    print("="*70)
    print(f"Total rows processed:        {stats['total_rows']}")
    print(f"Rows updated with new BINs:  {stats['updated']}")
    print(f"Rows already had BINs:       {stats['no_research']}")
    print(f"Rows not researched:         {stats['skipped']}")
    print("="*70 + "\n")

    if stats['updated'] > 0:
        print(f"✅ Dataset updated! New file: {output_file}")
        print(f"\nBefore next step, please verify the updated dataset:")
        print(f"  1. Review updated BINs in {output_file}")
        print(f"  2. If satisfied, run: mv {output_file} {main_file}")
        print(f"  3. Then run: ./scripts/load_clean_data.sh")
    else:
        print("⚠️  No BINs were updated. Check that bin_research_results.csv has 'real_bin' column filled.")

    print("")

    return 0


if __name__ == '__main__':
    exit(main())
