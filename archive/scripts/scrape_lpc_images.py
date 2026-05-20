#!/usr/bin/env python3
"""
Scrape high-quality architectural photos from NYC Landmarks Preservation Commission
Digital Photo Archive (Luna Imaging) and upload to Cloudflare R2.

The LPC archive has 98,000+ designation photos - official architectural photography
that's much higher quality than Google Street View.

Usage:
    python scripts/scrape_lpc_images.py --limit 100
    python scripts/scrape_lpc_images.py --limit 100 --dry-run
    python scripts/scrape_lpc_images.py --building "Chrysler Building"
"""

import os
import sys
import re
import argparse
import asyncio
import httpx
import psycopg2
from pathlib import Path
from typing import Optional, List, Dict
from urllib.parse import quote

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Load environment variables BEFORE importing config
from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

from models.config import get_settings
from utils.storage import s3_client

# LPC Luna Archive endpoints
LPC_BASE_URL = "https://nyclandmarks.lunaimaging.com"
LPC_SEARCH_URL = f"{LPC_BASE_URL}/luna/servlet/as/search"
LPC_MEDIA_URL = f"{LPC_BASE_URL}/luna/servlet/iiif"

# Image size for downloads (Luna supports: thumbnail, size1, size2, size3, size4, full)
IMAGE_SIZE = "size3"  # Good balance of quality vs file size


async def search_lpc_archive(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 10
) -> List[Dict]:
    """
    Search the LPC Luna archive for images matching a query.

    Args:
        query: Search term (building name, address, LP number)
        client: httpx async client
        limit: Max results to return

    Returns:
        List of image metadata dicts
    """
    try:
        # Luna search API
        params = {
            "q": query,
            "lc": "NYClandmarks~2~2",  # Collection ID
            "widgetType": "thumbnail",
            "os": 0,
            "bs": limit,
        }

        response = await client.get(
            f"{LPC_BASE_URL}/luna/servlet/as/search",
            params=params,
            headers={"Accept": "application/json"}
        )

        if response.status_code != 200:
            print(f"  ⚠️ Search failed: {response.status_code}")
            return []

        # Try to parse JSON response
        try:
            data = response.json()
            results = data.get("results", [])
            return results
        except:
            # Luna sometimes returns HTML, try parsing differently
            return []

    except Exception as e:
        print(f"  ❌ Search error: {e}")
        return []


async def get_iiif_image_url(media_id: str) -> str:
    """
    Construct IIIF image URL for downloading.

    Luna supports IIIF Image API, so we can request specific sizes.
    """
    # IIIF format: {base}/iiif/{id}/full/{size}/0/default.jpg
    return f"{LPC_BASE_URL}/luna/servlet/iiif/NYClandmarks~2~2~{media_id}/full/800,/0/default.jpg"


async def download_image(
    url: str,
    client: httpx.AsyncClient
) -> Optional[bytes]:
    """Download image from URL."""
    try:
        response = await client.get(url, timeout=30.0)
        if response.status_code == 200:
            return response.content
        else:
            print(f"  ⚠️ Download failed: {response.status_code}")
            return None
    except Exception as e:
        print(f"  ❌ Download error: {e}")
        return None


def upload_to_r2(
    image_bytes: bytes,
    bin_id: str,
    image_name: str = "lpc_facade.jpg"
) -> str:
    """
    Upload image to R2 bucket.

    Uses same folder structure as existing images: buildings/{bin}/{image_name}
    """
    settings = get_settings()

    key = f"buildings/{bin_id}/{image_name}"

    s3_client.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=image_bytes,
        ContentType='image/jpeg',
        ACL='public-read'
    )

    return f"{settings.r2_public_url}/{key}"


async def search_by_building_name(
    building_name: str,
    client: httpx.AsyncClient
) -> Optional[Dict]:
    """
    Search LPC archive by building name and return best match.
    """
    # Clean up building name for search
    search_term = building_name.strip()

    # Try direct search first
    results = await search_lpc_archive(search_term, client, limit=5)

    if results:
        return results[0]  # Return best match

    # Try simplified search (remove common suffixes)
    simplified = re.sub(r'\s+(Building|House|Mansion|Church|Hotel)$', '', search_term, flags=re.IGNORECASE)
    if simplified != search_term:
        results = await search_lpc_archive(simplified, client, limit=5)
        if results:
            return results[0]

    return None


async def process_building(
    building: Dict,
    client: httpx.AsyncClient,
    dry_run: bool = False
) -> bool:
    """
    Process a single building: search LPC, download image, upload to R2.

    Args:
        building: Dict with 'bin', 'building_name', 'address'
        client: httpx async client
        dry_run: If True, don't upload

    Returns:
        True if successful
    """
    bin_id = str(building['bin']).replace('.0', '')
    name = building.get('building_name') or ''
    address = building.get('address') or ''

    print(f"\n🏛️ {name or address} (BIN: {bin_id})")

    # Try searching by name first, then address
    search_terms = [name, address] if name else [address]

    image_data = None
    for term in search_terms:
        if not term:
            continue
        print(f"  🔍 Searching: {term[:50]}...")

        results = await search_lpc_archive(term, client, limit=5)

        if results:
            image_data = results[0]
            print(f"  ✅ Found {len(results)} results")
            break
        else:
            print(f"  ⚠️ No results")

    if not image_data:
        print(f"  ❌ No LPC images found")
        return False

    # Get image URL (this depends on Luna's response format)
    # Luna returns various formats, we need to extract the media ID
    media_id = image_data.get('id') or image_data.get('mediaId')

    if not media_id:
        # Try to extract from URL field
        url_field = image_data.get('urlSize4') or image_data.get('urlSize3') or image_data.get('thumbnail')
        if url_field:
            # URL format: /luna/servlet/iiif/NYClandmarks~2~2~123456/...
            match = re.search(r'NYClandmarks~\d+~\d+~(\d+)', url_field)
            if match:
                media_id = match.group(1)

    if not media_id:
        print(f"  ❌ Could not extract media ID")
        print(f"     Data: {image_data}")
        return False

    # Construct download URL
    image_url = await get_iiif_image_url(media_id)
    print(f"  📥 Downloading from IIIF...")

    if dry_run:
        print(f"  🔍 DRY RUN - Would download: {image_url}")
        return True

    # Download image
    image_bytes = await download_image(image_url, client)

    if not image_bytes:
        return False

    print(f"  ☁️ Uploading to R2...")

    # Upload to R2
    try:
        r2_url = upload_to_r2(image_bytes, bin_id, "lpc_facade.jpg")
        print(f"  ✅ Uploaded: {r2_url}")
        return True
    except Exception as e:
        print(f"  ❌ Upload error: {e}")
        return False


async def main():
    parser = argparse.ArgumentParser(
        description="Scrape LPC archive photos and upload to R2"
    )
    parser.add_argument("--limit", type=int, default=100, help="Max buildings to process")
    parser.add_argument("--dry-run", action="store_true", help="Don't upload to R2")
    parser.add_argument("--building", type=str, help="Process single building by name")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N buildings")
    args = parser.parse_args()

    settings = get_settings()

    # Connect to database
    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()

    if args.building:
        # Process single building
        cur.execute("""
            SELECT bin, building_name, address
            FROM buildings_full_merge_scanning
            WHERE building_name ILIKE %s
            LIMIT 1
        """, (f"%{args.building}%",))
    else:
        # Get buildings that might be landmarks (have building names)
        cur.execute("""
            SELECT bin, building_name, address
            FROM buildings_full_merge_scanning
            WHERE building_name IS NOT NULL
              AND building_name != ''
            ORDER BY building_name
            OFFSET %s
            LIMIT %s
        """, (args.offset, args.limit))

    buildings = cur.fetchall()

    print(f"📚 Found {len(buildings)} buildings to process")

    if args.dry_run:
        print("🔍 DRY RUN MODE - No uploads will occur")

    # Process buildings
    success_count = 0

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": "NYCLandmarksApp/1.0"}
    ) as client:

        for row in buildings:
            building = {
                'bin': row[0],
                'building_name': row[1],
                'address': row[2]
            }

            success = await process_building(building, client, dry_run=args.dry_run)
            if success:
                success_count += 1

            # Rate limiting - be nice to LPC servers
            await asyncio.sleep(0.5)

    conn.close()

    print("\n" + "="*60)
    print(f"✅ Done! Successfully processed {success_count}/{len(buildings)} buildings")
    if args.dry_run:
        print("🔍 DRY RUN - No images were uploaded")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
