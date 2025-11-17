#!/usr/bin/env python3
"""
Generate reference images for top 500 buildings using BIN-based folder structure.
Skips buildings that already have images in R2.
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
import warnings

# Suppress warnings
warnings.filterwarnings('ignore')

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
    except Exception:
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

            except Exception as e:
                print(f"  Error {angle}deg_{pitch}pitch: {e}")

            # Rate limiting
            await asyncio.sleep(0.3)  # 300ms between requests

    return success_count


def get_existing_bins(settings):
    """Get list of BINs that already have images in R2"""
    existing_bins = set()
    continuation_token = None

    while True:
        if continuation_token:
            response = s3_client.list_objects_v2(
                Bucket=settings.r2_bucket,
                Delimiter='/',
                ContinuationToken=continuation_token
            )
        else:
            response = s3_client.list_objects_v2(
                Bucket=settings.r2_bucket,
                Delimiter='/'
            )

        if 'CommonPrefixes' in response:
            for prefix in response['CommonPrefixes']:
                folder = prefix['Prefix'].rstrip('/')
                if folder.isdigit():
                    existing_bins.add(folder)

        if response.get('IsTruncated'):
            continuation_token = response.get('NextContinuationToken')
        else:
            break

    return existing_bins


async def main():
    """Generate reference images for top 500 buildings"""
    settings = get_settings()

    if not GOOGLE_API_KEY:
        print("GOOGLE_MAPS_API_KEY not found in environment")
        return 1

    # Load dataset
    print("Loading dataset...")
    df = pd.read_csv('data/final/full_dataset_fixed_bins.csv', low_memory=False)
    top_500 = df.nlargest(500, 'final_score')

    # Get existing BIN folders from R2
    print("Checking existing R2 images...")
    existing_bins = get_existing_bins(settings)
    print(f"Found {len(existing_bins)} existing BIN folders in R2")

    # Find buildings needing images
    missing = []
    for idx, row in top_500.iterrows():
        bin_val = str(row['bin']).replace('.0', '')

        if bin_val != 'nan' and bin_val not in existing_bins and bin_val != 'N/A':
            missing.append({
                'bin': bin_val,
                'lat': row['geocoded_lat'],
                'lng': row['geocoded_lng'],
                'name': str(row.get('building_name', row.get('address', 'Unknown'))),
                'address': str(row.get('address', 'Unknown')),
                'score': row['final_score']
            })

    total = len(missing)
    print(f"\nGenerating images for {total} buildings")
    print(f"Already have images for {500 - total} of top 500 buildings\n")

    if total == 0:
        print("All top 500 buildings already have reference images!")
        return 0

    # Estimate cost
    est_cost = total * 12 * 0.007  # 12 images per building at $0.007 each
    print(f"Estimated API cost: ${est_cost:.2f}")
    print(f"Press Ctrl+C to cancel...\n")
    await asyncio.sleep(3)  # Give time to cancel

    total_images = 0
    errors = 0

    for idx, building in enumerate(missing, 1):
        print(f"[{idx}/{total}] {building['name'][:50]} (score: {building['score']:.1f})")
        print(f"  BIN: {building['bin']} | {building['address'][:60]}")

        try:
            count = await cache_building(
                building['bin'],
                building['lat'],
                building['lng'],
                building['name'],
                settings
            )
            total_images += count
            print(f"  Generated {count}/12 images\n")

            if count == 0:
                errors += 1

        except Exception as e:
            print(f"  ERROR: {e}\n")
            errors += 1

        # Progress checkpoint every 50 buildings
        if idx % 50 == 0:
            print(f"--- Checkpoint: {idx}/{total} buildings processed ---")
            print(f"    Total images: {total_images}")
            print(f"    Errors: {errors}\n")

    print(f"\n=== SUMMARY ===")
    print(f"Total buildings processed: {total}")
    print(f"Total images generated: {total_images}")
    print(f"Buildings with errors: {errors}")
    print(f"Estimated actual cost: ${(total_images * 0.007):.2f}")
    print(f"\nNext step: Run generate_embeddings_bins.py to create CLIP embeddings")
    return 0


if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
