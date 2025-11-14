#!/usr/bin/env python3
"""
Script to apply BIN fixes to the dataset.

This handles:
1. Auto-marking public spaces as 'N/A'
2. Applying manual corrections from bin_fixes_template.csv
3. Validation and reporting
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Set
from collections import defaultdict


class BINFixer:
    """Apply BIN corrections to dataset."""

    PUBLIC_SPACE_KEYWORDS = {
        'park', 'pier', 'green', 'plaza', 'square', 'promenade',
        'waterfront', 'beach', 'bridge', 'tunnel', 'highway',
        'river', 'island', 'pool', 'playground', 'garden',
        'fence', 'gate', 'wall', 'pavilion', 'reservoir',
        'tract', 'lot', 'vacant', 'undeveloped', 'fort', 'station'
    }

    PLACEHOLDER_BINS = {1000000.0, 2000000.0, 3000000.0, 4000000.0, 5000000.0}

    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self.df = pd.read_csv(csv_path)
        self.bin_col = 'BIN'
        self.bbl_col = 'bbl'
        self.building_col = 'building_name'
        self.address_col = 'address'
        self.changes_made = []

    def is_public_space(self, building_name: str, address: str) -> bool:
        """Check if building appears to be a public space/park."""
        name = str(building_name).lower()
        addr = str(address).lower()
        combined = f"{name} {addr}"
        return any(keyword in combined for keyword in self.PUBLIC_SPACE_KEYWORDS)

    def is_placeholder_or_missing(self, bin_val) -> bool:
        """Check if BIN is missing or placeholder."""
        if pd.isna(bin_val) or bin_val == '' or bin_val == 'nan':
            return True
        try:
            return float(bin_val) in self.PLACEHOLDER_BINS
        except (ValueError, TypeError):
            return False

    def auto_fix_public_spaces(self) -> int:
        """Auto-mark all public spaces without real BINs as 'N/A'."""
        count = 0

        # Convert BIN column to string to avoid type mismatches
        self.df[self.bin_col] = self.df[self.bin_col].astype(str)

        for idx, row in self.df.iterrows():
            bin_val = row[self.bin_col]

            if self.is_placeholder_or_missing(bin_val):
                if self.is_public_space(row[self.building_col], row[self.address_col]):
                    old_bin = bin_val
                    self.df.at[idx, self.bin_col] = 'N/A'
                    count += 1
                    self.changes_made.append({
                        'bbl': row[self.bbl_col],
                        'building': row[self.building_col],
                        'old_bin': old_bin,
                        'new_bin': 'N/A',
                        'reason': 'PUBLIC_SPACE_AUTO_FIX'
                    })

        return count

    def apply_manual_fixes(self, fixes_csv: str) -> int:
        """Apply manual corrections from CSV."""
        try:
            fixes_df = pd.read_csv(fixes_csv)
        except FileNotFoundError:
            print(f"⚠️  No fixes file found at {fixes_csv}")
            return 0

        count = 0

        for idx, fix_row in fixes_df.iterrows():
            real_bin_val = fix_row.get('real_bin', '')
            real_bin = str(real_bin_val).strip() if pd.notna(real_bin_val) else ''

            # Skip empty entries or entries marked as "no fix"
            if not real_bin or real_bin.lower() in ['', 'skip', 'none']:
                continue

            # Find the building in our dataset
            bbl = fix_row.get('bbl')
            matching = self.df[self.df[self.bbl_col] == bbl]

            if len(matching) == 0:
                print(f"⚠️  BBL {bbl} not found in dataset")
                continue

            for match_idx in matching.index:
                old_bin = self.df.at[match_idx, self.bin_col]
                self.df.at[match_idx, self.bin_col] = real_bin
                count += 1
                self.changes_made.append({
                    'bbl': bbl,
                    'building': self.df.at[match_idx, self.building_col],
                    'old_bin': old_bin,
                    'new_bin': real_bin,
                    'reason': 'MANUAL_FIX'
                })

        return count

    def validate_and_report(self) -> Dict:
        """Validate the dataset after fixes and generate report."""
        report = {
            'total_buildings': len(self.df),
            'bins_with_real_value': 0,
            'bins_marked_na': 0,
            'bins_still_missing': 0,
            'duplicate_bins': 0,
            'changes_made': len(self.changes_made)
        }

        # Count BIN status
        for idx, row in self.df.iterrows():
            bin_val = row[self.bin_col]

            if pd.isna(bin_val) or bin_val == '' or bin_val == 'nan':
                report['bins_still_missing'] += 1
            elif bin_val == 'N/A':
                report['bins_marked_na'] += 1
            else:
                report['bins_with_real_value'] += 1

        # Count duplicates
        bin_counts = self.df[self.bin_col].value_counts()
        report['duplicate_bins'] = len(bin_counts[bin_counts > 1])

        return report

    def save_cleaned_dataset(self, output_path: str = None):
        """Save the cleaned dataset."""
        if output_path is None:
            input_path = Path(self.csv_path)
            output_path = input_path.parent / f"full_dataset_fixed_bins.csv"

        self.df.to_csv(output_path, index=False)
        print(f"✅ Cleaned dataset saved to: {output_path}")
        return output_path

    def save_changes_report(self):
        """Save a report of all changes made."""
        if not self.changes_made:
            return

        changes_df = pd.DataFrame(self.changes_made)
        report_path = Path(self.csv_path).parent / "bin_changes_applied.csv"
        changes_df.to_csv(report_path, index=False)
        print(f"✅ Changes report saved to: {report_path}")

    def print_report(self, validation: Dict):
        """Print summary report."""
        print("\n" + "=" * 100)
        print("BIN FIX APPLIED - VALIDATION REPORT")
        print("=" * 100)

        print(f"\n📊 RESULTS")
        print(f"{'─' * 100}")
        print(f"Total buildings:              {validation['total_buildings']:,}")
        print(f"With real BINs:               {validation['bins_with_real_value']:,} ({validation['bins_with_real_value']/validation['total_buildings']*100:.2f}%)")
        print(f"Marked as N/A (public):       {validation['bins_marked_na']:,} ({validation['bins_marked_na']/validation['total_buildings']*100:.2f}%)")
        print(f"Still missing BINs:           {validation['bins_still_missing']:,} ({validation['bins_still_missing']/validation['total_buildings']*100:.2f}%)")
        print(f"Duplicate BINs remaining:     {validation['duplicate_bins']:,}")
        print(f"\n✏️  Changes made:              {validation['changes_made']}")

        # Summary by reason
        if self.changes_made:
            reasons = defaultdict(int)
            for change in self.changes_made:
                reasons[change['reason']] += 1

            print(f"\n📋 Changes by Type:")
            for reason, count in sorted(reasons.items()):
                print(f"  - {reason}: {count}")


def main():
    """Run BIN fixing process."""
    csv_path = "data/final/full_dataset.csv"
    fixes_csv = "data/final/bin_fixes_template.csv"

    print("🔧 Applying BIN fixes to dataset...\n")

    fixer = BINFixer(csv_path)

    # Step 1: Auto-fix public spaces
    print("Step 1: Auto-marking public spaces as 'N/A'...")
    public_fixed = fixer.auto_fix_public_spaces()
    print(f"✅ Fixed {public_fixed} public spaces\n")

    # Step 2: Apply manual fixes (if template was filled in)
    print("Step 2: Applying manual fixes from template...")
    manual_fixed = fixer.apply_manual_fixes(fixes_csv)
    if manual_fixed > 0:
        print(f"✅ Applied {manual_fixed} manual fixes\n")
    else:
        print("ℹ️  No manual fixes found (template not yet filled in)\n")

    # Step 3: Validate and report
    print("Step 3: Validating cleaned dataset...")
    validation = fixer.validate_and_report()
    fixer.print_report(validation)

    # Step 4: Save cleaned dataset
    print("\nStep 4: Saving cleaned dataset...")
    output_path = fixer.save_cleaned_dataset()
    fixer.save_changes_report()

    print("\n" + "=" * 100)
    print("NEXT STEPS FOR COMPLETE MIGRATION:")
    print("=" * 100)
    print("""
1. ✅ PUBLIC SPACES: Auto-marked as 'N/A' (can't have BINs)
2. ⏳ MISSING BINs: Research using NYC DOB API
   - Use bin_fixes_template.csv as your research template
   - Fill in 'real_bin' column with found BINs
   - Rerun this script to apply fixes
   - NYC DOB BIS: https://a810-dobnow.nyc.gov/

3. 📊 Check validation above:
   - Still {} buildings missing BINs
   - Target: Get these to 0 or as close as possible

4. 🚀 Once satisfied with BIN coverage, proceed with database migration:
   - Create migration SQL script
   - Update application code (models, services, routers)
   - Test on staging environment
   - Deploy to production
""".format(validation['bins_still_missing']))


if __name__ == "__main__":
    main()
