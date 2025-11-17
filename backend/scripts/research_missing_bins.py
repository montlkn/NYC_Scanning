#!/usr/bin/env python3
"""
Research missing BINs - identifies public spaces and flags buildings needing manual lookup

This script takes buildings from data/final/bin_fixes_template.csv and:
1. Marks known public spaces as 'N/A'
2. Flags remaining buildings for manual research via NYC BIS Web
3. Generates bin_research_results.csv with clear instructions

Usage: python3 scripts/research_missing_bins.py
"""

import csv
import logging
from pathlib import Path
from typing import Dict

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Common NYC public spaces that don't have building IDs
PUBLIC_SPACES = {
    'PARK',
    'PIER',
    'PLAZA',
    'FERRY',
    'PARK SPACE',
    'PLAYGROUND',
    'WATERFRONT',
    'PUBLIC SPACE',
    'CENTRAL PARK',
    'PROSPECT PARK',
    'TIMES SQUARE',
    'UNION SQUARE',
    'MADISON SQUARE PARK',
    'WASHINGTON SQUARE PARK',
    'BATTERY PARK',
    'HUDSON RIVER PARK',
    'EAST RIVER PARK',
    'FLUSHING MEADOWS',
    'ROCKEFELLER CENTER PLAZA',
    'BRYANT PARK',
    'TOMPKINS SQUARE PARK',
    'SARA D ROOSEVELT PARK',
    'MARINERS PARK',
    'PIER 55',
    'PIER 57',
    'PIER 15',
    'PIER 17',
    'BATTERY WEED',
    'FORT WADSWORTH',
}


def is_public_space(address: str, building_name: str) -> bool:
    """Check if building is a public space based on address/name"""
    text = (address + " " + building_name).upper()
    return any(space in text for space in PUBLIC_SPACES)


def research_building(bbl: str, address: str, building_name: str) -> Dict:
    """
    Classify a building for research

    Returns dict with:
    - bbl: original BBL
    - address: building address
    - building_name: building name
    - status: 'PUBLIC_SPACE' or 'REQUIRES_MANUAL_RESEARCH'
    - real_bin: 'N/A' if public space, empty string for manual research
    - notes: additional information
    """

    # Check if public space
    if is_public_space(address, building_name):
        return {
            'bbl': bbl,
            'address': address,
            'building_name': building_name,
            'status': 'PUBLIC_SPACE',
            'real_bin': 'N/A',
            'notes': 'Marked as public space - no BIN available'
        }

    # Requires manual research
    return {
        'bbl': bbl,
        'address': address,
        'building_name': building_name,
        'status': 'REQUIRES_MANUAL_RESEARCH',
        'real_bin': '',
        'notes': 'Check NYC BIS Web for actual BIN'
    }


def main():
    """Main execution"""

    # Paths
    template_file = Path('data/final/bin_fixes_template.csv')
    output_file = Path('data/final/bin_research_results.csv')

    if not template_file.exists():
        logger.error(f"❌ Template file not found: {template_file}")
        return 1

    print("\n" + "="*70)
    print("NYC BUILDING BIN RESEARCH CLASSIFIER")
    print("="*70)
    print(f"Input:  {template_file}")
    print(f"Output: {output_file}\n")

    # Read template and classify buildings
    buildings_to_research = []
    with open(template_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Only research if current_bin is MISSING or a placeholder
            current_bin = str(row.get('current_bin', '')).strip()
            if current_bin in ['MISSING', '1000000.0', '2000000.0', '3000000.0', '4000000.0', '5000000.0']:
                buildings_to_research.append({
                    'bbl': row.get('bbl', '').strip(),
                    'building_name': row.get('building_name', '').strip(),
                    'address': row.get('address', '').strip(),
                })

    print(f"Found {len(buildings_to_research)} buildings needing research\n")

    # Research each building
    results = []
    stats = {
        'public_spaces': 0,
        'requires_manual': 0
    }

    for idx, building in enumerate(buildings_to_research, 1):
        result = research_building(
            bbl=building['bbl'],
            address=building['address'],
            building_name=building['building_name'],
        )

        results.append(result)

        # Update stats
        if result['status'] == 'PUBLIC_SPACE':
            stats['public_spaces'] += 1
            print(f"[{idx:2d}/{len(buildings_to_research)}] ✅ PUBLIC SPACE: {building['building_name']}")
        else:
            stats['requires_manual'] += 1
            print(f"[{idx:2d}/{len(buildings_to_research)}] ⚠️  RESEARCH:      {building['building_name']}")

    # Write results
    print(f"\nWriting results to {output_file}...")

    with open(output_file, 'w', newline='') as f:
        fieldnames = ['bbl', 'address', 'building_name', 'status', 'real_bin', 'notes']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # Print summary
    print("\n" + "="*70)
    print("CLASSIFICATION SUMMARY")
    print("="*70)
    print(f"Public Spaces (marked N/A):      {stats['public_spaces']:3d}")
    print(f"Requires Manual Research:        {stats['requires_manual']:3d}")
    print("="*70 + "\n")

    # Print buildings requiring manual research
    if stats['requires_manual'] > 0:
        print("BUILDINGS REQUIRING MANUAL BIN LOOKUP:")
        print("")
        manual_research = [r for r in results if r['status'] == 'REQUIRES_MANUAL_RESEARCH']
        for idx, r in enumerate(manual_research, 1):
            print(f"{idx:2d}. {r['building_name']}")
            print(f"    BBL: {r['bbl']} | Address: {r['address']}")

        print("\nTo find BINs for these buildings:")
        print("1. Visit: https://a810-bisweb.nyc.gov/bisweb/PropertyProfileHelp.html")
        print("2. Enter each BBL from the list above")
        print("3. Copy the BIN from the search result")
        print("4. Update bin_research_results.csv with real_bin value")
        print("")

    print(f"✅ Classification complete! Results saved to: {output_file}")
    print("\nNext steps:")
    print("1. Open bin_research_results.csv")
    print("2. For REQUIRES_MANUAL_RESEARCH rows, visit NYC BIS Web (link above)")
    print("3. Fill in the 'real_bin' column with values found")
    print("4. Run: python3 scripts/apply_bin_research.py")
    print("5. Run: ./scripts/load_clean_data.sh")
    print("")

    return 0


if __name__ == '__main__':
    exit(main())
