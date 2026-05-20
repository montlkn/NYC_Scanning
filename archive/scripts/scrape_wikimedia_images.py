#!/usr/bin/env python3
"""
Scrape architectural photos from Wikimedia Commons for NYC landmarks.

Uses the MediaWiki API to search for building images and download high-quality
versions for CLIP embedding.

Usage:
    python scripts/scrape_wikimedia_images.py --limit 100
    python scripts/scrape_wikimedia_images.py --limit 100 --dry-run
    python scripts/scrape_wikimedia_images.py --building "Empire State Building"
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
from urllib.parse import quote, unquote

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Load environment variables BEFORE importing config
from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

from models.config import get_settings
from utils.storage import s3_client

# Wikimedia Commons API
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"

# Preferred image width for downloads
IMAGE_WIDTH = 1024


async def search_commons(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 10
) -> List[Dict]:
    """
    Search Wikimedia Commons for images matching a query.

    Args:
        query: Search term (building name)
        client: httpx async client
        limit: Max results to return

    Returns:
        List of image metadata dicts
    """
    try:
        # Search for files
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": "6",  # File namespace
            "gsrsearch": f"{query} NYC building",
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "iiurlwidth": IMAGE_WIDTH,
        }

        response = await client.get(COMMONS_API_URL, params=params)

        if response.status_code != 200:
            print(f"  ⚠️ Search failed: {response.status_code}")
            return []

        data = response.json()

        if "query" not in data or "pages" not in data["query"]:
            return []

        results = []
        for page_id, page_data in data["query"]["pages"].items():
            if "imageinfo" in page_data and page_data["imageinfo"]:
                info = page_data["imageinfo"][0]
                results.append({
                    "title": page_data.get("title", ""),
                    "url": info.get("thumburl") or info.get("url"),
                    "original_url": info.get("url"),
                    "width": info.get("width", 0),
                    "height": info.get("height", 0),
                    "mime": info.get("mime", ""),
                })

        # Filter for JPEG images with reasonable dimensions
        results = [
            r for r in results
            if r["mime"] in ("image/jpeg", "image/png")
            and r["width"] >= 400
            and r["height"] >= 300
        ]

        return results

    except Exception as e:
        print(f"  ❌ Search error: {e}")
        return []


async def search_by_category(
    building_name: str,
    client: httpx.AsyncClient
) -> List[Dict]:
    """
    Search for a specific Commons category for the building.

    Many NYC landmarks have their own categories like "Category:Empire State Building"
    """
    try:
        # Clean building name for category search
        category_name = building_name.replace(" ", "_")

        params = {
            "action": "query",
            "format": "json",
            "generator": "categorymembers",
            "gcmtitle": f"Category:{category_name}",
            "gcmtype": "file",
            "gcmlimit": 20,
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "iiurlwidth": IMAGE_WIDTH,
        }

        response = await client.get(COMMONS_API_URL, params=params)

        if response.status_code != 200:
            return []

        data = response.json()

        if "query" not in data or "pages" not in data["query"]:
            return []

        results = []
        for page_id, page_data in data["query"]["pages"].items():
            if "imageinfo" in page_data and page_data["imageinfo"]:
                info = page_data["imageinfo"][0]
                title = page_data.get("title", "").lower()

                # Prefer exterior/facade shots
                is_exterior = any(word in title for word in [
                    "exterior", "facade", "front", "building", "view",
                    "street", "outside"
                ])
                is_interior = any(word in title for word in [
                    "interior", "lobby", "inside", "room", "ceiling"
                ])

                results.append({
                    "title": page_data.get("title", ""),
                    "url": info.get("thumburl") or info.get("url"),
                    "original_url": info.get("url"),
                    "width": info.get("width", 0),
                    "height": info.get("height", 0),
                    "mime": info.get("mime", ""),
                    "is_exterior": is_exterior and not is_interior,
                    "score": 2 if is_exterior else (0 if is_interior else 1),
                })

        # Filter and sort by preference
        results = [
            r for r in results
            if r["mime"] in ("image/jpeg", "image/png")
            and r["width"] >= 400
        ]

        results.sort(key=lambda x: (-x["score"], -x["width"]))

        return results

    except Exception as e:
        print(f"  ❌ Category search error: {e}")
        return []


async def download_image(
    url: str,
    client: httpx.AsyncClient
) -> Optional[bytes]:
    """Download image from URL."""
    try:
        response = await client.get(url, timeout=30.0, follow_redirects=True)
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
    image_name: str = "wikimedia_facade.jpg"
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


async def process_building(
    building: Dict,
    client: httpx.AsyncClient,
    dry_run: bool = False
) -> bool:
    """
    Process a single building: search Wikimedia, download image, upload to R2.
    """
    bin_id = str(building['bin']).replace('.0', '')
    name = building.get('building_name') or ''
    address = building.get('address') or ''

    if not name:
        return False

    print(f"\n🏛️ {name} (BIN: {bin_id})")

    # Try category search first (higher quality, curated)
    print(f"  🔍 Searching category...")
    results = await search_by_category(name, client)

    if not results:
        # Fall back to general search
        print(f"  🔍 Searching general...")
        results = await search_commons(name, client, limit=10)

    if not results:
        print(f"  ❌ No Wikimedia images found")
        return False

    # Get best result
    best = results[0]
    print(f"  ✅ Found: {best['title'][:60]}...")

    if dry_run:
        print(f"  🔍 DRY RUN - Would download: {best['url'][:80]}...")
        return True

    # Download image
    print(f"  📥 Downloading...")
    image_bytes = await download_image(best['url'], client)

    if not image_bytes:
        return False

    print(f"  ☁️ Uploading to R2...")

    # Upload to R2
    try:
        r2_url = upload_to_r2(image_bytes, bin_id, "wikimedia_facade.jpg")
        print(f"  ✅ Uploaded: {r2_url}")
        return True
    except Exception as e:
        print(f"  ❌ Upload error: {e}")
        return False


async def main():
    parser = argparse.ArgumentParser(
        description="Scrape Wikimedia Commons photos and upload to R2"
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
        # Prioritize famous buildings
        cur.execute("""
            SELECT bin, building_name, address
            FROM buildings_full_merge_scanning
            WHERE building_name IS NOT NULL
              AND building_name != ''
            ORDER BY
                CASE
                    WHEN building_name ILIKE '%empire state%' THEN 1
                    WHEN building_name ILIKE '%chrysler%' THEN 2
                    WHEN building_name ILIKE '%rockefeller%' THEN 3
                    WHEN building_name ILIKE '%grand central%' THEN 4
                    ELSE 5
                END,
                building_name
            OFFSET %s
            LIMIT %s
        """, (args.offset, args.limit))

    buildings = cur.fetchall()

    print(f"📚 Found {len(buildings)} buildings to process")

    if args.dry_run:
        print("🔍 DRY RUN MODE - No uploads will occur")

    # Process buildings in batches for speed
    success_count = 0
    batch_size = 10  # Process 10 buildings concurrently

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={
            "User-Agent": "NYCLandmarksApp/1.0 (https://github.com/yourrepo; contact@email.com)"
        }
    ) as client:

        for i in range(0, len(buildings), batch_size):
            batch = buildings[i:i + batch_size]

            # Process batch concurrently
            tasks = []
            for row in batch:
                building = {
                    'bin': row[0],
                    'building_name': row[1],
                    'address': row[2]
                }
                tasks.append(process_building(building, client, dry_run=args.dry_run))

            # Wait for batch to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Count successes
            for result in results:
                if result is True:
                    success_count += 1

            # Progress
            print(f"📊 Processed {min(i + batch_size, len(buildings))}/{len(buildings)} buildings ({success_count} successes)")

            # Rate limiting between batches
            await asyncio.sleep(2.0)

    conn.close()

    print("\n" + "="*60)
    print(f"✅ Done! Successfully processed {success_count}/{len(buildings)} buildings")
    if args.dry_run:
        print("🔍 DRY RUN - No images were uploaded")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
