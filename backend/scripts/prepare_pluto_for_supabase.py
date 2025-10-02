#!/usr/bin/env python3
"""
Prepare PLUTO CSV for direct Supabase import

Creates a clean CSV with only the columns we need
"""

import csv
import sys

# Borough mapping
BOROUGH_MAP = {
    "MN": "Manhattan", "1": "Manhattan",
    "BX": "Bronx", "2": "Bronx",
    "BK": "Brooklyn", "3": "Brooklyn",
    "QN": "Queens", "4": "Queens",
    "SI": "Staten Island", "5": "Staten Island",
}

def prepare_csv(input_file, output_file):
    """Extract only needed columns from PLUTO"""

    with open(input_file, 'r') as infile, open(output_file, 'w', newline='') as outfile:
        reader = csv.DictReader(infile)

        # Output columns matching our table
        fieldnames = [
            'bbl', 'address', 'borough', 'zip_code',
            'latitude', 'longitude', 'num_floors', 'year_built',
            'building_class', 'land_use', 'lot_area', 'building_area',
            'is_landmark', 'scan_enabled', 'data_source'
        ]

        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()

        processed = 0
        skipped = 0

        for row in reader:
            # Skip if missing critical data
            bbl = row.get('BBL', '').strip()
            lat = row.get('latitude', '').strip()
            lng = row.get('longitude', '').strip()
            address = row.get('address', '').strip()

            if not (bbl and lat and lng and address and lat != '0' and lng != '0'):
                skipped += 1
                continue

            borough = BOROUGH_MAP.get(row.get('borough', '').strip().upper(), 'Unknown')

            # Round num_floors to integer
            num_floors = row.get('numfloors', '').strip()
            if num_floors:
                try:
                    num_floors = str(int(float(num_floors)))
                except:
                    num_floors = ''

            # Round year_built to integer
            year_built = row.get('yearbuilt', '').strip()
            if year_built:
                try:
                    year_built = str(int(float(year_built)))
                except:
                    year_built = ''

            writer.writerow({
                'bbl': bbl,
                'address': address,
                'borough': borough,
                'zip_code': row.get('postcode', '').strip()[:5] or '',
                'latitude': lat,
                'longitude': lng,
                'num_floors': num_floors,
                'year_built': year_built,
                'building_class': row.get('bldgclass', '').strip() or '',
                'land_use': row.get('landuse', '').strip() or '',
                'lot_area': row.get('lotarea', '').strip() or '',
                'building_area': row.get('bldgarea', '').strip() or '',
                'is_landmark': 'false',
                'scan_enabled': 'true',
                'data_source': '{pluto}'
            })

            processed += 1
            if processed % 100000 == 0:
                print(f"Processed {processed} rows...")

        print(f"\n✅ Created {output_file}")
        print(f"   Processed: {processed}")
        print(f"   Skipped: {skipped}")

if __name__ == '__main__':
    prepare_csv(
        'data/Primary_Land_Use_Tax_Lot_Output__PLUTO__20251001.csv',
        'data/pluto_for_supabase.csv'
    )
