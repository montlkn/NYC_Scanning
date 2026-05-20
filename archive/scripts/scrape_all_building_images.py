#!/usr/bin/env python3
"""
Master image scraper - combines all sources to get the best available image
for EVERY building in buildings_full_merge_scanning.

Priority order:
1. LPC Luna Archive (98k official landmark photos)
2. Wikimedia Commons (crowd-sourced, often high quality)
3. Keep existing Street View as fallback

For each building, we try sources in order until we find a good image.

Usage:
    python scripts/scrape_all_building_images.py --limit 100 --dry-run
    python scripts/scrape_all_building_images.py --limit 1000
    python scripts/scrape_all_building_images.py  # Process all buildings
"""

import os
import sys
import re
import argparse
import asyncio
import httpx
import psycopg2
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from urllib.parse import quote
from PIL import Image
from io import BytesIO

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Load environment variables BEFORE importing config
from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

from models.config import get_settings
from utils.storage import s3_client

# API endpoints
LPC_BASE_URL = "https://nyclandmarks.lunaimaging.com"
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"

# Minimum image dimensions
MIN_WIDTH = 400
MIN_HEIGHT = 300

# Stats tracking
stats = {
    "total": 0,
    "lpc_success": 0,
    "wikimedia_success": 0,
    "no_image_found": 0,
    "already_has_image": 0,
    "errors": 0,
}


def check_existing_image(bin_id: str, settings) -> bool:
    """Check if building already has a good quality image in R2."""
    try:
        # Check for LPC or Wikimedia image (not street view)
        for filename in ["lpc_facade.jpg", "wikimedia_facade.jpg"]:
            key = f"buildings/{bin_id}/{filename}"
            try:
                s3_client.head_object(Bucket=settings.r2_bucket, Key=key)
                return True
            except:
                continue
        return False
    except:
        return False


# ============================================================================
# LPC Luna Archive
# ============================================================================

async def search_lpc(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 5,
    verbose: bool = False
) -> List[Dict]:
    """Search LPC Luna archive."""
    try:
        # Simplified params - just query and collection
        params = {
            "q": query,
            "lc": "NYClandmarks~2~2",
        }

        response = await client.get(
            f"{LPC_BASE_URL}/luna/servlet/as/search",
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
        )

        if verbose:
            print(f"    🔍 LPC search: '{query}' -> {response.status_code}")

        if response.status_code != 200:
            return []

        try:
            content_type = response.headers.get('content-type', '')
            if 'json' not in content_type.lower():
                if verbose:
                    print(f"    ⚠️  Response is not JSON (type: {content_type})")
                    print(f"    📄 First 200 chars: {response.text[:200]}")
                return []

            data = response.json()
            total = data.get("totalResults", 0)
            results = data.get("results", [])

            if verbose:
                if results:
                    print(f"    ✅ Found {len(results)} results (total: {total})")
                else:
                    print(f"    ⚠️  0 results (API returned: totalResults={total})")

            return results
        except Exception as e:
            if verbose:
                print(f"    ❌ Parse error: {e}")
                print(f"    📄 Response preview: {response.text[:200]}")
            return []

    except Exception as e:
        if verbose:
            print(f"    ❌ Request error: {e}")
        return []


async def download_lpc_image(
    media_id: str,
    client: httpx.AsyncClient
) -> Optional[bytes]:
    """Download image from LPC using IIIF."""
    url = f"{LPC_BASE_URL}/luna/servlet/iiif/NYClandmarks~2~2~{media_id}/full/800,/0/default.jpg"
    try:
        response = await client.get(url, timeout=30.0)
        if response.status_code == 200 and len(response.content) > 5000:
            return response.content
    except:
        pass
    return None


async def try_lpc(
    building_name: str,
    address: str,
    bin_id: str,
    client: httpx.AsyncClient,
    verbose: bool = False
) -> Optional[bytes]:
    """Try to get image from LPC archive with fallback strategy."""
    search_terms = []

    # PRIORITY 1: Address (most reliable in LPC)
    if address:
        search_terms.append(("address", address))
        # Try just street number + street name
        street_match = re.match(r'(\d+[A-Z]?)\s+(.+)', address)
        if street_match:
            number = street_match.group(1)
            street = street_match.group(2)
            search_terms.append(("address_split", f"{number} {street}"))

    # PRIORITY 2: Building name
    if building_name:
        search_terms.append(("name", building_name))
        # Try simplified name
        simplified = re.sub(r'\s+(Building|House|Mansion|Church|Hotel|Company|No\.\s*\d+)$', '', building_name, flags=re.IGNORECASE)
        if simplified != building_name and simplified.strip():
            search_terms.append(("name_simplified", simplified))

    # PRIORITY 3: BIN (format as LP-XXXXX which is LPC landmark number)
    # Note: BIN != LP number, but we can try searching by BIN in text
    if bin_id:
        search_terms.append(("bin", bin_id))

    for search_type, term in search_terms:
        if not term or not term.strip():
            continue

        results = await search_lpc(term, client, verbose=verbose)
        if not results:
            continue

        # Found results! Try to download
        if verbose:
            print(f"    📥 Trying to download from {len(results)} results...")

        for i, result in enumerate(results[:3]):  # Try top 3 results
            media_id = result.get('id') or result.get('mediaId')

            if not media_id:
                url_field = result.get('urlSize4') or result.get('urlSize3') or result.get('thumbnail')
                if verbose and i == 0:
                    print(f"    🔧 Result keys: {list(result.keys())[:5]}")
                    if url_field:
                        print(f"    🔧 URL field: {url_field[:60]}...")

                if url_field:
                    match = re.search(r'NYClandmarks~\d+~\d+~(\d+)', url_field)
                    if match:
                        media_id = match.group(1)

            if media_id:
                if verbose and i == 0:
                    print(f"    🔧 Extracted media_id: {media_id}")
                image_bytes = await download_lpc_image(media_id, client)
                if image_bytes:
                    if verbose:
                        print(f"    ✅ Downloaded image (media_id: {media_id})")
                    return image_bytes
                elif verbose and i == 0:
                    print(f"    ❌ Download failed for media_id: {media_id}")
            elif verbose and i == 0:
                print(f"    ❌ Could not extract media_id from result")

    return None


# ============================================================================
# Wikimedia Commons
# ============================================================================

async def search_wikimedia(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 10
) -> List[Dict]:
    """Search Wikimedia Commons."""
    try:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": "6",
            "gsrsearch": f"{query} NYC building exterior",
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "iiurlwidth": 1024,
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
                if info.get("mime") in ("image/jpeg", "image/png"):
                    results.append({
                        "url": info.get("thumburl") or info.get("url"),
                        "width": info.get("width", 0),
                        "height": info.get("height", 0),
                    })

        # Filter by size
        results = [r for r in results if r["width"] >= MIN_WIDTH and r["height"] >= MIN_HEIGHT]
        results.sort(key=lambda x: -x["width"])

        return results

    except:
        return []


async def try_wikimedia(
    building_name: str,
    address: str,
    client: httpx.AsyncClient
) -> Optional[bytes]:
    """Try to get image from Wikimedia Commons."""
    search_terms = []
    if building_name:
        search_terms.append(building_name)
    if address:
        # Extract street number and name for search
        search_terms.append(address)

    for term in search_terms:
        if not term:
            continue

        results = await search_wikimedia(term, client)
        if not results:
            continue

        # Try downloading best result
        for result in results[:3]:
            try:
                response = await client.get(result["url"], timeout=30.0, follow_redirects=True)
                if response.status_code == 200 and len(response.content) > 5000:
                    return response.content
            except:
                continue

    return None


# ============================================================================
# Main Processing
# ============================================================================

def upload_to_r2(
    image_bytes: bytes,
    bin_id: str,
    source: str  # "lpc" or "wikimedia"
) -> str:
    """Upload image to R2."""
    settings = get_settings()

    filename = f"{source}_facade.jpg"
    key = f"buildings/{bin_id}/{filename}"

    # Verify it's a valid image and convert to JPEG
    try:
        img = Image.open(BytesIO(image_bytes))
        # Convert to RGB if necessary (for PNG with alpha)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        # Save as JPEG
        output = BytesIO()
        img.save(output, format='JPEG', quality=85, optimize=True)
        output.seek(0)
        image_bytes = output.read()
    except Exception as e:
        raise ValueError(f"Invalid image: {e}")

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
    settings,
    dry_run: bool = False,
    skip_existing: bool = True
) -> Tuple[bool, str]:
    """
    Process a single building through all image sources.

    Returns:
        (success, source) - source is "lpc", "wikimedia", "existing", or "none"
    """
    bin_id = str(building['bin']).replace('.0', '')
    name = building.get('building_name') or ''
    address = building.get('address') or ''

    # Check if already has a good image
    if skip_existing and check_existing_image(bin_id, settings):
        return (True, "existing")

    # Try LPC first (highest quality)
    verbose = True  # Enable verbose for now
    image_bytes = await try_lpc(name, address, bin_id, client, verbose=verbose)
    if image_bytes:
        source = "lpc"
    else:
        # Try Wikimedia
        image_bytes = await try_wikimedia(name, address, client)
        if image_bytes:
            source = "wikimedia"
        else:
            return (False, "none")

    if dry_run:
        return (True, source)

    # Upload to R2
    try:
        upload_to_r2(image_bytes, bin_id, source)
        return (True, source)
    except Exception as e:
        return (False, "error")


async def main():
    parser = argparse.ArgumentParser(
        description="Scrape best available images for all buildings"
    )
    parser.add_argument("--limit", type=int, help="Max buildings to process (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Don't upload to R2")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N buildings")
    parser.add_argument("--no-skip-existing", action="store_true", help="Re-process buildings with existing images")
    parser.add_argument("--batch-size", type=int, default=50, help="Process in batches")
    args = parser.parse_args()

    settings = get_settings()

    # Connect to database
    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()

    # Count total buildings
    cur.execute("SELECT COUNT(*) FROM buildings_full_merge_scanning")
    total_buildings = cur.fetchone()[0]

    print(f"📊 Total buildings in database: {total_buildings}")

    # Get buildings to process
    limit_clause = f"LIMIT {args.limit}" if args.limit else ""

    cur.execute(f"""
        SELECT bin, building_name, address
        FROM buildings_full_merge_scanning
        ORDER BY
            -- Prioritize named buildings (landmarks)
            CASE WHEN building_name IS NOT NULL AND building_name != '' THEN 0 ELSE 1 END,
            building_name
        OFFSET {args.offset}
        {limit_clause}
    """)

    buildings = cur.fetchall()
    total_to_process = len(buildings)

    print(f"🏗️ Buildings to process: {total_to_process}")

    if args.dry_run:
        print("🔍 DRY RUN MODE - No uploads will occur")

    skip_existing = not args.no_skip_existing

    # Process in batches
    success_count = 0
    lpc_count = 0
    wikimedia_count = 0
    existing_count = 0
    failed_count = 0

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": "NYCLandmarksApp/1.0"}
    ) as client:

        for i, row in enumerate(buildings):
            building = {
                'bin': row[0],
                'building_name': row[1],
                'address': row[2]
            }

            bin_id = str(building['bin']).replace('.0', '')
            name = building.get('building_name') or building.get('address') or bin_id

            # Progress indicator
            if (i + 1) % 10 == 0 or i == 0:
                print(f"\n[{i + 1}/{total_to_process}] Processing: {name[:40]}...")

            success, source = await process_building(
                building, client, settings,
                dry_run=args.dry_run,
                skip_existing=skip_existing
            )

            if success:
                success_count += 1
                if source == "lpc":
                    lpc_count += 1
                    print(f"  ✅ LPC: {name[:40]}")
                elif source == "wikimedia":
                    wikimedia_count += 1
                    print(f"  ✅ Wikimedia: {name[:40]}")
                elif source == "existing":
                    existing_count += 1
            else:
                failed_count += 1
                if (i + 1) % 50 == 0:  # Only print failures occasionally
                    print(f"  ❌ No image: {name[:40]}")

            # Rate limiting
            await asyncio.sleep(0.3)

            # Progress summary every 100 buildings
            if (i + 1) % 100 == 0:
                print(f"\n📊 Progress: {i + 1}/{total_to_process}")
                print(f"   LPC: {lpc_count}, Wikimedia: {wikimedia_count}, Existing: {existing_count}, Failed: {failed_count}")

    conn.close()

    print("\n" + "="*60)
    print("✅ COMPLETE!")
    print("="*60)
    print(f"📊 Total processed: {total_to_process}")
    print(f"   ✅ LPC images: {lpc_count}")
    print(f"   ✅ Wikimedia images: {wikimedia_count}")
    print(f"   ⏭️ Already had images: {existing_count}")
    print(f"   ❌ No image found: {failed_count}")

    if args.dry_run:
        print("\n🔍 DRY RUN - No images were uploaded")

    coverage = ((lpc_count + wikimedia_count + existing_count) / total_to_process * 100) if total_to_process > 0 else 0
    print(f"\n📈 Coverage: {coverage:.1f}%")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
