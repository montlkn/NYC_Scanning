import requests
import asyncio
import math
import os
import numpy as np
from PIL import Image
from io import BytesIO
import py360convert
import boto3
import psycopg2

GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')
R2_ENDPOINT = f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
SCAN_DB_URL = os.getenv('SCAN_DB_URL')

s3 = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY')
)

def offset_position(lat, lng, bearing, distance_m):
    R = 6371000
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

def fetch_panorama(lat, lng):
    # First check if Street View is available
    metadata_url = "https://maps.googleapis.com/maps/api/streetview/metadata"
    metadata_params = {
        'location': f'{lat},{lng}',
        'key': GOOGLE_API_KEY
    }
    metadata_response = requests.get(metadata_url, params=metadata_params)
    metadata = metadata_response.json()
    
    if metadata.get('status') != 'OK':
        return None
    
    # Fetch the actual image
    url = "https://maps.googleapis.com/maps/api/streetview"
    params = {
        'location': f'{lat},{lng}',
        'size': '640x640',
        'fov': 120,
        'key': GOOGLE_API_KEY
    }
    response = requests.get(url, params=params)
    
    try:
        img = Image.open(BytesIO(response.content))
        # Verify it's a real image (not error page)
        img.verify()
        # Reopen after verify
        img = Image.open(BytesIO(response.content))
        return img
    except:
        return None

async def cache_building(building_id, bbl, center_lat, center_lng):
    angles = [0, 90, 180, 270]
    pitches = [0, 20]
    distance = 40
    success_count = 0
    
    for angle in angles:
        cam_lat, cam_lng = offset_position(center_lat, center_lng, angle, distance)
        
        try:
            panorama = fetch_panorama(cam_lat, cam_lng)
            
            if panorama is None:
                print(f"  ⊘ angle {angle}: no Street View coverage")
                continue
            
            heading_to_building = (angle + 180) % 360
            
            for pitch in pitches:
                view = py360convert.e2p(
                    np.array(panorama),
                    fov_deg=(90, 90),
                    u_deg=heading_to_building,
                    v_deg=pitch,
                    out_hw=(512, 512),
                    mode='bilinear'
                )
                
                key = f"{bbl}/{angle}deg_{pitch}pitch.jpg"
                buffer = BytesIO()
                Image.fromarray(view.astype('uint8')).save(buffer, format='JPEG', quality=85)
                buffer.seek(0)
                
                s3.upload_fileobj(
                    buffer, 
                    os.getenv('R2_BUCKET'),
                    key,
                    ExtraArgs={'ContentType': 'image/jpeg'}
                )
                
                success_count += 1
            
            print(f"  ✓ angle {angle}")
            await asyncio.sleep(0.5)
            
        except Exception as e:
            print(f"  ✗ angle {angle}: {e}")
    
    return success_count

async def main():
    conn = psycopg2.connect(SCAN_DB_URL)
    cur = conn.cursor()
    cur.execute("SELECT id, bbl, ST_Y(center) as lat, ST_X(center) as lng FROM buildings WHERE tier=1")
    
    buildings = cur.fetchall()
    total = len(buildings)
    print(f"Caching {total} buildings...")
    print()
    
    total_success = 0
    for idx, row in enumerate(buildings, 1):
        building_id, bbl, lat, lng = row
        print(f"[{idx}/{total}] {bbl}")
        count = await cache_building(building_id, bbl, lat, lng)
        total_success += count
    
    print()
    print(f"Cached {total_success} images total")
    conn.close()

if __name__ == '__main__':
    asyncio.run(main())
