#!/usr/bin/env python3
"""
Analyze BIN coverage in the full dataset CSV

Usage:
    python scripts/analyze_bin_coverage.py --csv /path/to/full_dataset.csv
"""

import csv
import sys
from pathlib import Path
from collections import defaultdict
import argparse


def analyze_bin_coverage(csv_path: str):
    """
    Analyze BIN field coverage in CSV

    Returns statistics about BIN presence and shows examples
    """

    if not Path(csv_path).exists():
        print(f"❌ File not found: {csv_path}")
        sys.exit(1)

    print(f"📂 Analyzing: {csv_path}")
    print("=" * 80)

    # Statistics
    total_rows = 0
    rows_with_bin = 0
    rows_without_bin = 0
    duplicate_bbls = defaultdict(list)
    duplicate_bins = defaultdict(int)

    # Sample rows
    sample_with_bin = []
    sample_without_bin = []

    # Full list of buildings without BIN
    buildings_without_bin = []

    # Track which column is BIN
    bin_column = None
    bbl_column = None

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            # Find BIN column (case-insensitive)
            fieldnames = reader.fieldnames
            print(f"📋 CSV Columns: {', '.join(fieldnames[:10])}{'...' if len(fieldnames) > 10 else ''}")
            print()

            for field in fieldnames:
                if field.upper() == 'BIN':
                    bin_column = field
                if field.upper() == 'BBL':
                    bbl_column = field

            if not bin_column:
                print("⚠️  WARNING: No 'BIN' column found in CSV!")
                print(f"   Available columns: {', '.join(fieldnames)}")
                print()
                # Try to find it anyway
                for field in fieldnames:
                    if 'bin' in field.lower():
                        print(f"   Found possible BIN column: '{field}'")
                        bin_column = field
                        break

            if not bbl_column:
                print("⚠️  WARNING: No 'BBL' column found in CSV!")
                for field in fieldnames:
                    if 'bbl' in field.lower():
                        print(f"   Found possible BBL column: '{field}'")
                        bbl_column = field
                        break

            if not bin_column:
                print("❌ Cannot proceed without BIN column")
                sys.exit(1)

            print(f"✅ Using BIN column: '{bin_column}'")
            if bbl_column:
                print(f"✅ Using BBL column: '{bbl_column}'")
            print()

            # Analyze rows
            for row in reader:
                total_rows += 1

                bin_value = str(row.get(bin_column, '')).strip()
                bbl_value = str(row.get(bbl_column, '')).strip() if bbl_column else ''

                # Check if BIN exists and is not empty/null
                if bin_value and bin_value.lower() not in ['', 'null', 'none', 'nan', '0']:
                    rows_with_bin += 1

                    # Track duplicate BBLs
                    if bbl_value:
                        duplicate_bbls[bbl_value].append(bin_value)
                        duplicate_bins[bin_value] += 1

                    # Sample
                    if len(sample_with_bin) < 5:
                        sample_with_bin.append({
                            'BBL': bbl_value,
                            'BIN': bin_value,
                            'Address': row.get('address', row.get('Address', ''))[:50]
                        })
                else:
                    rows_without_bin += 1

                    # Track all buildings without BIN
                    buildings_without_bin.append({
                        'BBL': bbl_value,
                        'BIN': bin_value,
                        'Address': row.get('address', row.get('Address', ''))[:70],
                        'Borough': row.get('borough', row.get('Borough', '')),
                    })

                    # Sample
                    if len(sample_without_bin) < 5:
                        sample_without_bin.append({
                            'BBL': bbl_value,
                            'BIN': bin_value,
                            'Address': row.get('address', row.get('Address', ''))[:50]
                        })

                # Progress
                if total_rows % 100000 == 0:
                    print(f"  Processed {total_rows:,} rows...")

        # Calculate statistics
        pct_with_bin = (rows_with_bin / total_rows * 100) if total_rows > 0 else 0
        pct_without_bin = (rows_without_bin / total_rows * 100) if total_rows > 0 else 0

        # Find BBLs with multiple BINs (like WTC)
        multi_building_lots = {bbl: bins for bbl, bins in duplicate_bbls.items() if len(bins) > 1}

        # Find duplicate BINs (should be rare/impossible)
        duplicate_bin_list = {bin_val: count for bin_val, count in duplicate_bins.items() if count > 1}

        # Print results
        print()
        print("=" * 80)
        print("📊 BIN COVERAGE ANALYSIS")
        print("=" * 80)
        print(f"Total buildings:           {total_rows:,}")
        print(f"Buildings WITH BIN:        {rows_with_bin:,} ({pct_with_bin:.2f}%)")
        print(f"Buildings WITHOUT BIN:     {rows_without_bin:,} ({pct_without_bin:.2f}%)")
        print()

        # Multi-building lots
        if multi_building_lots:
            print("🏢 BBLs with Multiple Buildings:")
            print("-" * 80)
            for bbl, bins in list(multi_building_lots.items())[:10]:
                unique_bins = list(set(bins))[:5]  # Convert to list first
                print(f"  BBL {bbl}: {len(bins)} buildings (BINs: {', '.join(unique_bins)})")
            if len(multi_building_lots) > 10:
                print(f"  ... and {len(multi_building_lots) - 10} more")
            print()

        # Duplicate BINs (error check)
        if duplicate_bin_list:
            print("⚠️  WARNING: Duplicate BINs Found (should be unique!):")
            print("-" * 80)
            for bin_val, count in list(duplicate_bin_list.items())[:5]:
                print(f"  BIN {bin_val}: appears {count} times")
            print()

        # Samples
        print("✅ Sample Buildings WITH BIN:")
        print("-" * 80)
        for sample in sample_with_bin:
            print(f"  BBL: {sample['BBL']:<12} BIN: {sample['BIN']:<10} {sample['Address']}")

        print()
        print("❌ Sample Buildings WITHOUT BIN:")
        print("-" * 80)
        for sample in sample_without_bin:
            print(f"  BBL: {sample['BBL']:<12} BIN: {sample['BIN']:<10} {sample['Address']}")

        # Full list of buildings without BIN
        if buildings_without_bin:
            print()
            print("=" * 80)
            print(f"📋 COMPLETE LIST OF {len(buildings_without_bin)} BUILDINGS WITHOUT BIN")
            print("=" * 80)
            for i, building in enumerate(buildings_without_bin, 1):
                print(f"{i:3d}. BBL: {building['BBL']:<12} Borough: {building['Borough']:<15} Address: {building['Address']}")
            print()

        print()
        print("=" * 80)
        print("SUMMARY")
        print("=" * 80)

        if pct_with_bin >= 95:
            print("✅ EXCELLENT: BIN coverage is excellent (>95%)")
            print("   ➜ Safe to migrate to BIN as primary identifier")
        elif pct_with_bin >= 80:
            print("⚠️  GOOD: BIN coverage is good (80-95%)")
            print("   ➜ Migration possible but consider fallback for missing BINs")
        elif pct_with_bin >= 50:
            print("⚠️  FAIR: BIN coverage is fair (50-80%)")
            print("   ➜ Consider hybrid approach or data enrichment")
        else:
            print("❌ POOR: BIN coverage is poor (<50%)")
            print("   ➜ Need to enrich data or stick with BBL")

        print()
        print(f"Multi-building lots: {len(multi_building_lots):,} BBLs have multiple buildings")
        print(f"   These REQUIRE BIN to distinguish individual buildings")
        print()

        if rows_without_bin > 0:
            print("📋 Missing BIN Strategy:")
            print("   Option 1: Use composite key (id + BBL + BIN)")
            print("   Option 2: Generate synthetic BIN for missing values")
            print("   Option 3: Accept missing BINs, use BBL as fallback")

        print("=" * 80)

        # Return stats for programmatic use
        return {
            'total': total_rows,
            'with_bin': rows_with_bin,
            'without_bin': rows_without_bin,
            'pct_coverage': pct_with_bin,
            'multi_building_lots': len(multi_building_lots),
            'duplicate_bins': len(duplicate_bin_list)
        }

    except Exception as e:
        print(f"❌ Error analyzing CSV: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Analyze BIN coverage in NYC building dataset')
    parser.add_argument('--csv', required=True, help='Path to CSV file')

    args = parser.parse_args()

    analyze_bin_coverage(args.csv)


if __name__ == '__main__':
    main()
