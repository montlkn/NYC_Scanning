#!/usr/bin/env python3
"""
Preprocess landmarks CSV to extract lat/lng from geometry field

Usage:
    python scripts/preprocess_landmarks.py --input data/walk_optimized_landmarks.csv --output data/landmarks_with_coords.csv
"""

import csv
import re
import argparse
from typing import Optional, Tuple
from pyproj import Transformer

# NYC State Plane to WGS84 transformer
# EPSG:2263 (NYS State Plane Long Island) → EPSG:4326 (WGS84)
transformer = Transformer.from_crs("EPSG:2263", "EPSG:4326", always_xy=True)


def extract_coords_from_geometry(geom_str: str) -> Optional[Tuple[float, float]]:
    """
    Extract coordinates from geometry string
    Format: MULTIPOLYGON (((x y, x y, ...)))
    Returns: (latitude, longitude) in WGS84
    """
    try:
        # Extract all coordinate pairs
        coords = re.findall(r'([\d.]+)\s+([\d.]+)', geom_str)
        if not coords:
            return None

        # Get first coordinate (building centroid approximation)
        x, y = float(coords[0][0]), float(coords[0][1])

        # Transform from State Plane to lat/lng
        lng, lat = transformer.transform(x, y)

        return (lat, lng)

    except Exception as e:
        print(f"Error parsing geometry: {e}")
        return None


def preprocess_csv(input_path: str, output_path: str):
    """Process landmarks CSV to add lat/lng columns"""

    processed = 0
    skipped = 0

    with open(input_path, 'r', encoding='utf-8') as infile, \
         open(output_path, 'w', newline='', encoding='utf-8') as outfile:

        reader = csv.DictReader(infile)

        # Add latitude/longitude columns
        fieldnames = list(reader.fieldnames) + ['latitude', 'longitude']
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            geom = row.get('geometry', '')
            coords = extract_coords_from_geometry(geom)

            if coords:
                row['latitude'] = coords[0]
                row['longitude'] = coords[1]
                writer.writerow(row)
                processed += 1
            else:
                skipped += 1
                if skipped <= 5:
                    print(f"⚠️ Skipped: {row.get('des_addres', 'N/A')} (no valid geometry)")

    print(f"\n✅ Processed {processed} landmarks")
    print(f"⚠️  Skipped {skipped} landmarks (invalid geometry)")
    print(f"📄 Output: {output_path}")


def main():
    parser = argparse.ArgumentParser(description='Preprocess landmarks CSV')
    parser.add_argument('--input', required=True, help='Input CSV path')
    parser.add_argument('--output', required=True, help='Output CSV path')

    args = parser.parse_args()

    print("=" * 60)
    print("Landmarks CSV Preprocessing")
    print("=" * 60)
    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    print("=" * 60)
    print()

    preprocess_csv(args.input, args.output)


if __name__ == '__main__':
    main()
