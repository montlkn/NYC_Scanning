#!/usr/bin/env python3
"""
Generate reference images for top 100 buildings using BIN-based folder structure.
"""

import requests
import asyncio
import math
import os
import sys
from pathlib import Path
from PIL import Image
from io import BytesIO
import pandas as pd

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from models.config import get_settings
from utils.storage import s3_client
from dotenv import load_dotenv

load_dotenv()

GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

def offset_position(lat, lng, bearing, distance_m):
    """Calculate new lat/lng position offset from a point by distance and bearing"""
    R = 6371000  # Earth radius in meters
    d = distance_m / R
    bearing_rad = math.radians(bearing)
    lat_rad = math.radians(lat)
    lng_rad = math.radians(lng)

    new_lat = math.asin(
        math.sin(lat_rad) * math.cos(d) +
        math.cos(lat_rad) * math.sin(d) * math.cos(bearing_rad)
    )
    new_lng = lng_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(d) * math.cos(lat_rad),
        math.cos(d) - math.sin(lat_rad) * math.sin(new_lat)
    )

    return math.degrees(new_lat), math.degrees(new_lng)

def fetch_street_view(lat, lng, heading, pitch, size='640x640'):
    """Fetch Street View image from Google Maps API"""
    # Check if available
    metadata_url = "https://maps.googleapis.com/maps/api/streetview/metadata"
    metadata_params = {
        'location': f'{lat},{lng}',
        'source': 'outdoor',
        'key': GOOGLE_API_KEY
    }
    metadata_response = requests.get(metadata_url, params=metadata_params)
    metadata = metadata_response.json()

    if metadata.get('status') != 'OK':
        return None

    # Fetch image
    url = "https://maps.googleapis.com/maps/api/streetview"
    params = {
        'location': f'{lat},{lng}',
        'size': size,
        'heading': heading,
        'pitch': pitch,
        'fov': 90,
        'source': 'outdoor',
        'key': GOOGLE_API_KEY
    }
    response = requests.get(url, params=params)

    try:
        img = Image.open(BytesIO(response.content))
        img.verify()
        img = Image.open(BytesIO(response.content))
        return img
    except:
        return None

async def cache_building(bin_val, lat, lng, name, settings):
    """Generate and cache reference images for a building"""
    angles = [0, 90, 180, 270]
    pitches = [0, 20, 40]
    distance = 40  # meters from building center
    success_count = 0

    for angle in angles:
        # Calculate camera position offset from building center
        cam_lat, cam_lng = offset_position(lat, lng, angle, distance)
        # Calculate heading to point camera at building
        heading_to_building = (angle + 180) % 360

        for pitch in pitches:
            try:
                img = fetch_street_view(cam_lat, cam_lng, heading_to_building, pitch)

                if img is None:
                    continue

                # Upload to R2 with BIN-based path
                key = f"{bin_val}/{angle}deg_{pitch}pitch.jpg"
                buffer = BytesIO()
                img.save(buffer, format='JPEG', quality=90)
                buffer.seek(0)

                s3_client.upload_fileobj(
                    buffer,
                    settings.r2_bucket,
                    key,
                    ExtraArgs={'ContentType': 'image/jpeg'}
                )

                success_count += 1
                print(f"  ✓ {angle}deg_{pitch}pitch")

            except Exception as e:
                print(f"  ✗ {angle}deg_{pitch}pitch: {e}")

            # Rate limiting
            await asyncio.sleep(0.5)

    return success_count

async def main():
    """Generate reference images for top 100 buildings"""
    settings = get_settings()

    if not GOOGLE_API_KEY:
        print("❌ GOOGLE_MAPS_API_KEY not found in environment")
        return 1

    # Load dataset
    print("Loading dataset...")
    df = pd.read_csv('data/final/full_dataset_fixed_bins.csv')
    top_100 = df.nlargest(100, 'final_score')

    # Get existing BIN folders from R2
    print("Checking existing R2 images...")
    response = s3_client.list_objects_v2(
        Bucket=settings.r2_bucket,
        Delimiter='/'
    )

    existing_bins = set()
    if 'CommonPrefixes' in response:
        for prefix in response['CommonPrefixes']:
            folder = prefix['Prefix'].rstrip('/')
            if folder.isdigit():
                existing_bins.add(folder)

    print(f"Found {len(existing_bins)} existing BIN folders in R2")

    # Find buildings needing images
    missing = []
    for idx, row in top_100.iterrows():
        bin_val = str(row['bin']).replace('.0', '')

        if bin_val not in existing_bins and bin_val != 'N/A':
            missing.append({
                'bin': bin_val,
                'lat': row['geocoded_lat'],
                'lng': row['geocoded_lng'],
                'name': row['building_name'],
                'address': row['address']
            })

    total = len(missing)
    print(f"\n🏗️  Generating images for {total} buildings from top 100")
    print(f"Already have images for {100 - total} buildings\n")

    if total == 0:
        print("✅ All top 100 buildings already have reference images!")
        return 0

    total_images = 0
    for idx, building in enumerate(missing, 1):
        print(f"[{idx}/{total}] {building['name'][:50]}")
        print(f"  BIN: {building['bin']} | {building['address'][:60]}")

        count = await cache_building(
            building['bin'],
            building['lat'],
            building['lng'],
            building['name'],
            settings
        )
        total_images += count
        print(f"  Generated {count}/12 images\n")

    print(f"✅ Generated {total_images} total images for {total} buildings")
    return 0

if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
