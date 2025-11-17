#!/usr/bin/env python3
"""
Find missing BINs using coordinates from BUILDING data

For buildings without BINs, use their lat/lng to find nearby buildings
in the BUILDING dataset that DO have BINs.

Strategy:
1. Load all buildings with coordinates from BUILDING_20251104.csv
2. For each missing BIN in our dataset, find closest building by distance
3. If within reasonable distance (e.g., <30 meters), use that BIN
4. Otherwise mark as COORDINATE_LOOKUP_REQUIRED

Usage: python3 scripts/find_bins_by_coordinates.py
"""

import csv
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance between two coordinates in meters"""
    R = 6371000  # Earth radius in meters

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)

    a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c

def load_building_coords(building_csv: Path) -> Dict[str, Tuple[float, float, str]]:
    """Load buildings with their coords and BINs from official NYC data

    Returns: {(lat, lng): (bin, name)}
    """
    coords_to_bin = {}

    with open(building_csv, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Get coordinates and BIN
                geom = row.get('the_geom', '').strip()
                bin_val = str(row.get('BIN', '')).strip()
                name = row.get('NAME', '').strip()

                if not bin_val or not geom or geom.startswith('POLYGON'):
                    continue

                # Extract center coords from MULTIPOLYGON
                # Format: MULTIPOLYGON (((-73.xxx 40.xxx, ...)))
                try:
                    # Get first coordinate pair
                    import re
                    coords = re.findall(r'\((-?\d+\.?\d*)\s(-?\d+\.?\d*)', geom)
                    if coords:
                        lng, lat = float(coords[0][0]), float(coords[0][1])
                        key = (round(lat, 6), round(lng, 6))
                        if key not in coords_to_bin:
                            coords_to_bin[key] = (bin_val, name)
                except:
                    pass
            except:
                pass

    return coords_to_bin

def find_nearest_bin(lat: float, lng: float, coords_to_bin: Dict, max_distance: float = 30) -> Optional[str]:
    """Find nearest building BIN within max_distance meters"""

    nearest_bin = None
    nearest_distance = float('inf')

    for (coord_lat, coord_lng), (bin_val, _) in coords_to_bin.items():
        distance = haversine_distance(lat, lng, coord_lat, coord_lng)

        if distance < nearest_distance and distance < max_distance:
            nearest_distance = distance
            nearest_bin = bin_val

    return nearest_bin

def main():
    """Main execution"""

    # Paths
    building_csv = Path('data/BUILDING_20251104.csv')
    research_csv = Path('data/final/bin_research_results_with_building_data.csv')
    main_file = Path('data/final/full_dataset_fixed_bins.csv')
    output_file = Path('data/final/full_dataset_with_coord_lookup.csv')

    if not building_csv.exists():
        print(f"❌ Building CSV not found: {building_csv}")
        return 1

    if not main_file.exists():
        print(f"❌ Main dataset not found: {main_file}")
        return 1

    print("\n" + "="*70)
    print("FINDING MISSING BINs USING COORDINATES")
    print("="*70 + "\n")

    # Load research results to know which BBLs need BINs
    missing_bbls = set()
    with open(research_csv, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            real_bin = str(row.get('real_bin', '')).strip()
            if not real_bin:
                bbl = str(row.get('bbl', '')).strip().split('.')[0]
                missing_bbls.add(bbl)

    print(f"Buildings needing BIN research: {len(missing_bbls)}\n")

    # Load official building coordinates
    print("Loading NYC Building coordinates...")
    coords_to_bin = load_building_coords(building_csv)
    print(f"Loaded {len(coords_to_bin):,} building coordinates\n")

    # Process main dataset
    print("Processing main dataset...")
    stats = {'updated': 0, 'coord_lookup': 0, 'not_found': 0}

    rows = []
    with open(main_file, 'r') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames

        for row in reader:
            bin_val = str(row.get('bin', '')).strip()
            bbl = str(row.get('bbl', '')).strip().split('.')[0] if 'bbl' in row else ''

            # If building is in missing list and doesn't have BIN
            if bbl in missing_bbls and not bin_val:
                try:
                    lat = float(row.get('latitude', 0))
                    lng = float(row.get('longitude', 0))

                    if lat and lng:
                        found_bin = find_nearest_bin(lat, lng, coords_to_bin, max_distance=30)

                        if found_bin:
                            row['bin'] = found_bin
                            stats['updated'] += 1
                            print(f"✅ {bbl}: Found BIN {found_bin} via coordinates ({lat:.4f}, {lng:.4f})")
                        else:
                            stats['coord_lookup'] += 1
                            print(f"⚠️  {bbl}: No BIN found within 30m of ({lat:.4f}, {lng:.4f})")
                    else:
                        stats['not_found'] += 1
                except Exception as e:
                    stats['not_found'] += 1

            rows.append(row)

    # Write output
    print(f"\nWriting results to {output_file}...")
    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    print("\n" + "="*70)
    print("COORDINATE-BASED BIN LOOKUP RESULTS")
    print("="*70)
    print(f"Found via coordinates:          {stats['updated']}")
    print(f"Require manual lookup:          {stats['coord_lookup']}")
    print(f"Could not process:              {stats['not_found']}")
    print("="*70 + "\n")

    if stats['updated'] > 0:
        print(f"✅ Successfully found {stats['updated']} additional BINs!")
        print(f"Results saved to: {output_file}\n")
        print("Next: mv full_dataset_with_coord_lookup.csv full_dataset_fixed_bins.csv")

    return 0

if __name__ == '__main__':
    exit(main())
