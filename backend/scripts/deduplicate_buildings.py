#!/usr/bin/env python3
"""
Deduplicate buildings dataset before BIN migration

Identifies and resolves duplicate BINs and duplicate BBLs with different addresses
"""

import csv
import sys
from pathlib import Path
from collections import defaultdict
import argparse


def analyze_duplicates(csv_path: str):
    """
    Analyze duplicate buildings in the dataset
    """

    print(f"📂 Analyzing duplicates in: {csv_path}")
    print("=" * 80)

    # Track duplicates
    bin_to_rows = defaultdict(list)
    bbl_to_rows = defaultdict(list)

    rows = []

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames

        for i, row in enumerate(reader):
            rows.append((i, row))

            bin_val = str(row.get('BIN', row.get('bin', ''))).strip()
            bbl_val = str(row.get('bbl', row.get('BBL', ''))).strip()

            if bin_val and bin_val.lower() not in ['', 'null', 'none', 'nan']:
                bin_to_rows[bin_val].append((i, row))

            if bbl_val:
                bbl_to_rows[bbl_val].append((i, row))

    # Find duplicates
    duplicate_bins = {bin_val: row_list for bin_val, row_list in bin_to_rows.items() if len(row_list) > 1}
    duplicate_bbls_diff_addr = {}

    for bbl, row_list in bbl_to_rows.items():
        if len(row_list) > 1:
            # Check if addresses are different
            addresses = set(r[1].get('address', r[1].get('Address', '')) for r in row_list)
            if len(addresses) > 1:
                duplicate_bbls_diff_addr[bbl] = row_list

    print(f"Total rows: {len(rows):,}")
    print(f"Duplicate BINs: {len(duplicate_bins)}")
    print(f"Duplicate BBLs (different addresses): {len(duplicate_bbls_diff_addr)}")
    print()

    # Show duplicate BINs
    if duplicate_bins:
        print("🚨 DUPLICATE BINs (should be unique!):")
        print("=" * 80)
        for bin_val, row_list in list(duplicate_bins.items())[:10]:
            print(f"\nBIN: {bin_val} ({len(row_list)} occurrences)")
            for idx, row in row_list:
                print(f"  Row {idx}: BBL={row.get('bbl', ''):<15} Address: {row.get('address', row.get('Address', ''))[:60]}")
        print()

    # Show duplicate BBLs with different addresses
    if duplicate_bbls_diff_addr:
        print("🚨 DUPLICATE BBLs with DIFFERENT ADDRESSES:")
        print("=" * 80)
        for bbl, row_list in list(duplicate_bbls_diff_addr.items())[:10]:
            print(f"\nBBL: {bbl} ({len(row_list)} different buildings)")
            for idx, row in row_list:
                bin_val = row.get('BIN', row.get('bin', ''))
                print(f"  Row {idx}: BIN={bin_val:<12} Address: {row.get('address', row.get('Address', ''))[:60]}")
        print()

    return duplicate_bins, duplicate_bbls_diff_addr, rows, fieldnames


def deduplicate_csv(csv_path: str, output_path: str, strategy: str = 'keep_first'):
    """
    Remove duplicates from CSV

    Strategies:
    - keep_first: Keep first occurrence of duplicate BIN
    - keep_best: Keep row with most complete data
    - interactive: Ask user for each duplicate
    """

    duplicate_bins, duplicate_bbls, rows, fieldnames = analyze_duplicates(csv_path)

    if not duplicate_bins:
        print("✅ No duplicate BINs found - data is clean!")
        return

    print(f"🔧 Deduplication strategy: {strategy}")
    print()

    # Track which rows to keep
    rows_to_remove = set()

    # Handle duplicate BINs
    for bin_val, dup_rows in duplicate_bins.items():
        if strategy == 'keep_first':
            # Keep first, mark others for removal
            for idx, row in dup_rows[1:]:
                rows_to_remove.add(idx)
                print(f"❌ Removing row {idx}: BIN={bin_val}, BBL={row.get('bbl', '')}, Address={row.get('address', '')[:50]}")

        elif strategy == 'keep_best':
            # Score each row by completeness
            def score_row(row):
                score = 0
                important_fields = ['building_name', 'architect', 'style', 'year_built', 'height', 'landmark_name']
                for field in important_fields:
                    val = str(row.get(field, '')).strip()
                    if val and val.lower() not in ['', 'null', 'none', 'nan']:
                        score += 1
                return score

            # Sort by score, keep best
            scored = [(score_row(r[1]), i, r) for i, r in enumerate(dup_rows)]
            scored.sort(reverse=True)

            print(f"BIN {bin_val}: Keeping best of {len(dup_rows)} duplicates")
            for score, i, (idx, row) in scored:
                if i == 0:
                    print(f"  ✅ KEEPING row {idx} (score={score}): {row.get('address', '')[:50]}")
                else:
                    rows_to_remove.add(idx)
                    print(f"  ❌ REMOVING row {idx} (score={score}): {row.get('address', '')[:50]}")

    # Write deduplicated CSV
    print()
    print(f"💾 Writing deduplicated data to: {output_path}")

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        written = 0
        for idx, row in rows:
            if idx not in rows_to_remove:
                writer.writerow(row)
                written += 1

    print()
    print("=" * 80)
    print("DEDUPLICATION COMPLETE")
    print("=" * 80)
    print(f"Original rows:    {len(rows):,}")
    print(f"Removed:          {len(rows_to_remove):,}")
    print(f"Final rows:       {written:,}")
    print(f"Output file:      {output_path}")
    print()

    # Re-analyze to confirm
    print("🔍 Verifying deduplicated data...")
    dup_bins_after, _, _, _ = analyze_duplicates(output_path)

    if not dup_bins_after:
        print("✅ SUCCESS: No duplicate BINs remain!")
    else:
        print(f"⚠️  WARNING: Still {len(dup_bins_after)} duplicate BINs found")


def main():
    parser = argparse.ArgumentParser(description='Deduplicate buildings CSV')
    parser.add_argument('--csv', required=True, help='Input CSV file')
    parser.add_argument('--output', help='Output CSV file (default: input_deduplicated.csv)')
    parser.add_argument('--analyze-only', action='store_true', help='Only analyze, don\'t deduplicate')
    parser.add_argument('--strategy', choices=['keep_first', 'keep_best'], default='keep_best',
                       help='Deduplication strategy (default: keep_best)')

    args = parser.parse_args()

    if args.analyze_only:
        analyze_duplicates(args.csv)
    else:
        output = args.output or args.csv.replace('.csv', '_deduplicated.csv')
        deduplicate_csv(args.csv, output, args.strategy)


if __name__ == '__main__':
    main()
