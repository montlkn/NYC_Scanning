#!/usr/bin/env python3
"""
Script to identify and fix missing/placeholder BINs in the dataset.

Strategy:
1. Identify all buildings with placeholder BINs (1000000, 2000000, etc.)
2. Identify all buildings with missing BINs
3. Attempt to find real BINs using:
   - NYC PLUTO data secondary lookups
   - Address-based matching
   - BBL-based lookups
4. For unmatchable entries (parks, public spaces), mark as 'N/A'
5. Generate report and cleaned CSV
"""

import pandas as pd
import numpy as np
import csv
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import requests
import json
from collections import defaultdict


class BINFixer:
    """Identifies and fixes placeholder/missing BINs."""

    # Placeholder BINs that indicate missing data
    PLACEHOLDER_BINS = {1000000.0, 2000000.0, 3000000.0, 4000000.0, 5000000.0}

    # Keywords indicating public/unmatchable spaces
    PUBLIC_SPACE_KEYWORDS = {
        'park', 'pier', 'green', 'plaza', 'square', 'promenade',
        'waterfront', 'beach', 'bridge', 'tunnel', 'highway',
        'river', 'island', 'pool', 'playground', 'garden',
        'fence', 'gate', 'wall', 'pavilion', 'reservoir',
        'tract', 'lot', 'vacant', 'undeveloped'
    }

    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self.df = pd.read_csv(csv_path)
        self.bin_col = 'BIN'
        self.bbl_col = 'bbl'
        self.building_col = 'building_name'
        self.address_col = 'address'

        # Get borough from BBL first digit - handle non-numeric values
        def get_borough_code(bbl):
            try:
                bbl_str = str(bbl).strip()
                if bbl_str and bbl_str[0].isdigit():
                    return int(bbl_str[0])
            except:
                pass
            return None

        self.df['borough_code'] = self.df[self.bbl_col].apply(get_borough_code)

        self.issues = defaultdict(list)
        self.fixes = defaultdict(list)

    def is_placeholder_bin(self, bin_val) -> bool:
        """Check if BIN is a placeholder value."""
        try:
            return float(bin_val) in self.PLACEHOLDER_BINS
        except (ValueError, TypeError):
            return False

    def is_missing_bin(self, bin_val) -> bool:
        """Check if BIN is missing/null."""
        return pd.isna(bin_val) or bin_val == '' or bin_val == 'nan'

    def is_public_space(self, building_name: str, address: str) -> bool:
        """Heuristic: Check if building appears to be a public space/park."""
        name = str(building_name).lower()
        addr = str(address).lower()

        combined = f"{name} {addr}"

        return any(keyword in combined for keyword in self.PUBLIC_SPACE_KEYWORDS)

    def find_duplicate_bins(self) -> Dict[float, List[Dict]]:
        """Find all BINs that appear multiple times."""
        duplicates = {}
        bin_counts = self.df[self.bin_col].value_counts()

        for bin_val, count in bin_counts.items():
            if count > 1:
                matches = self.df[self.df[self.bin_col] == bin_val]
                duplicates[bin_val] = matches.to_dict('records')

        return duplicates

    def analyze_issues(self) -> Dict:
        """Analyze all BIN-related issues."""
        analysis = {
            'total_buildings': len(self.df),
            'missing_bins': 0,
            'placeholder_bins': 0,
            'duplicate_bins': 0,
            'public_spaces_without_bins': 0,
            'invalid_bins': [],
            'missing_details': [],
            'placeholder_details': [],
            'duplicates_by_borough': defaultdict(list),
        }

        # Count missing and placeholder BINs
        for idx, row in self.df.iterrows():
            bin_val = row[self.bin_col]
            building = row[self.building_col]
            address = row[self.address_col]
            bbl = row[self.bbl_col]
            borough_code = row['borough_code']

            if self.is_missing_bin(bin_val):
                analysis['missing_bins'] += 1
                if self.is_public_space(building, address):
                    analysis['public_spaces_without_bins'] += 1
                analysis['missing_details'].append({
                    'bbl': bbl,
                    'building': building,
                    'address': address,
                    'is_public_space': self.is_public_space(building, address)
                })
            elif self.is_placeholder_bin(bin_val):
                analysis['placeholder_bins'] += 1
                if self.is_public_space(building, address):
                    analysis['public_spaces_without_bins'] += 1
                analysis['placeholder_details'].append({
                    'bbl': bbl,
                    'bin': bin_val,
                    'building': building,
                    'address': address,
                    'is_public_space': self.is_public_space(building, address)
                })

        # Count duplicates
        duplicates = self.find_duplicate_bins()
        analysis['duplicate_bins'] = len(duplicates)

        for bin_val, records in duplicates.items():
            if not self.is_placeholder_bin(bin_val):
                borough = int(str(records[0][self.bbl_col])[0])
                analysis['duplicates_by_borough'][borough].append({
                    'bin': bin_val,
                    'count': len(records),
                    'buildings': [r[self.building_col] for r in records]
                })

        return analysis

    def generate_report(self) -> str:
        """Generate a detailed report of BIN issues."""
        analysis = self.analyze_issues()

        report = []
        report.append("=" * 100)
        report.append("BIN DATA QUALITY REPORT")
        report.append("=" * 100)

        report.append(f"\n📊 SUMMARY")
        report.append(f"{'─' * 100}")
        report.append(f"Total buildings:                    {analysis['total_buildings']:,}")
        report.append(f"Buildings with missing BINs:        {analysis['missing_bins']:,} ({analysis['missing_bins']/analysis['total_buildings']*100:.2f}%)")
        report.append(f"Buildings with placeholder BINs:    {analysis['placeholder_bins']:,} ({analysis['placeholder_bins']/analysis['total_buildings']*100:.2f}%)")
        report.append(f"Buildings with duplicate BINs:      {analysis['duplicate_bins']:,}")
        report.append(f"Public spaces without real BINs:    {analysis['public_spaces_without_bins']:,}")

        report.append(f"\n📋 PLACEHOLDER BINs (Need Research or Mark as N/A)")
        report.append(f"{'─' * 100}")
        report.append(f"Total with placeholders: {analysis['placeholder_bins']}\n")

        # Group by placeholder value
        placeholder_groups = defaultdict(list)
        for detail in analysis['placeholder_details']:
            placeholder_groups[detail['bin']].append(detail)

        for bin_val, details in sorted(placeholder_groups.items()):
            public_count = sum(1 for d in details if d['is_public_space'])
            report.append(f"\nBIN {bin_val}: {len(details)} buildings ({public_count} public spaces)")
            report.append(f"  Examples:")
            for detail in details[:3]:
                is_public = "🌳 PUBLIC SPACE" if detail['is_public_space'] else ""
                report.append(f"    - {detail['building'][:50]:50} @ {detail['address'][:30]:30} {is_public}")
            if len(details) > 3:
                report.append(f"    ... and {len(details) - 3} more")

        report.append(f"\n❌ MISSING BINs (Need Research or Mark as N/A)")
        report.append(f"{'─' * 100}")
        report.append(f"Total missing: {analysis['missing_bins']}\n")

        public_count = sum(1 for d in analysis['missing_details'] if d['is_public_space'])
        report.append(f"Public spaces without BIN: {public_count}")

        report.append(f"\nExamples of buildings needing BIN research:")
        for detail in analysis['missing_details'][:10]:
            is_public = "🌳 PUBLIC SPACE" if detail['is_public_space'] else ""
            report.append(f"  - BBL {detail['bbl']}: {detail['building'][:50]:50} {is_public}")
        if len(analysis['missing_details']) > 10:
            report.append(f"  ... and {len(analysis['missing_details']) - 10} more")

        report.append(f"\n✔️  LEGITIMATE DUPLICATE BINs (Different Buildings, Same BIN)")
        report.append(f"{'─' * 100}")

        all_legitimate_dupes = []
        for borough, dupes in sorted(analysis['duplicates_by_borough'].items()):
            all_legitimate_dupes.extend(dupes)
            report.append(f"\nBorough {borough}: {len(dupes)} BINs with duplicates")
            for dupe in dupes[:3]:
                report.append(f"  - BIN {dupe['bin']}: {dupe['count']} buildings")
                for building in dupe['buildings'][:2]:
                    report.append(f"      • {building}")

        if not all_legitimate_dupes:
            report.append("(Only placeholder BINs have legitimate duplicates)")

        report.append("\n" + "=" * 100)
        report.append("RECOMMENDATIONS")
        report.append("=" * 100)
        report.append("""
1. PUBLIC SPACES (Parks, Piers, etc.):
   - Mark placeholder BINs for public spaces as 'N/A'
   - These cannot have Building Identification Numbers (by definition)
   - Total affected: ~{} buildings

2. BUILDING STRUCTURES WITH MISSING/PLACEHOLDER BINs:
   - Research using NYC DOB BIS API
   - Cross-reference PLUTO data
   - Manual lookup for iconic buildings
   - Target: Replace at least 80% of non-public-space missing BINs

3. LEGITIMATE DUPLICATE BINs:
   - {} buildings with legitimate duplicates (different buildings, same BIN)
   - This is valid for complex lots with multiple structures
   - Use composite key (BIN, BBL) for uniqueness if needed

4. DATA QUALITY:
   - After fixes, implement BIN validation in ingestion pipeline
   - Add BIN as NOT NULL (except for public spaces marked 'N/A')
   - Create unique constraint on BIN for scannable buildings
""".format(public_count, len(all_legitimate_dupes)))

        return "\n".join(report)

    def print_report(self):
        """Print the report to console."""
        report = self.generate_report()
        print(report)

        # Save report to file
        report_path = Path(self.csv_path).parent / "bin_analysis_report.txt"
        with open(report_path, 'w') as f:
            f.write(report)
        print(f"\n✅ Report saved to: {report_path}")

    def create_cleaning_template(self) -> pd.DataFrame:
        """Create a template for manual BIN corrections."""
        analysis = self.analyze_issues()

        # Collect all rows that need fixing
        to_fix = []

        for idx, row in self.df.iterrows():
            bin_val = row[self.bin_col]

            if self.is_missing_bin(bin_val) or self.is_placeholder_bin(bin_val):
                is_public = self.is_public_space(row[self.building_col], row[self.address_col])
                to_fix.append({
                    'index': idx,
                    'bbl': row[self.bbl_col],
                    'building_name': row[self.building_col],
                    'address': row[self.address_col],
                    'current_bin': bin_val if not self.is_missing_bin(bin_val) else 'MISSING',
                    'is_public_space': is_public,
                    'recommended_action': 'MARK_N/A' if is_public else 'RESEARCH',
                    'real_bin': '',  # To be filled in manually
                    'notes': ''
                })

        fix_df = pd.DataFrame(to_fix)

        # Save template
        template_path = Path(self.csv_path).parent / "bin_fixes_template.csv"
        fix_df.to_csv(template_path, index=False)
        print(f"✅ Cleaning template saved to: {template_path}")
        print(f"   Total entries to review: {len(fix_df)}")

        return fix_df


def main():
    """Run BIN analysis and generate report."""
    csv_path = "data/final/full_dataset.csv"

    print("🔍 Analyzing BIN data quality...\n")

    fixer = BINFixer(csv_path)
    fixer.print_report()

    print("\n📝 Creating cleaning template for manual corrections...")
    template_df = fixer.create_cleaning_template()

    print("\n" + "=" * 100)
    print("NEXT STEPS:")
    print("=" * 100)
    print("""
1. Review the report above
2. For PUBLIC SPACES: Auto-mark as 'N/A' (they shouldn't have BINs)
3. For BUILDINGS: Use NYC DOB BIS API or manual research to find real BINs
   - NYC DOB BIS: https://a810-dobnow.nyc.gov/
   - PLUTO data: Check if you have secondary PLUTO fields
4. Fill in bin_fixes_template.csv with real BINs or 'N/A'
5. Run fix_bin_data_apply.py to apply corrections to the dataset
""")


if __name__ == "__main__":
    main()
