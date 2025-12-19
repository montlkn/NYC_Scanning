#!/usr/bin/env python3
"""
TURBO MODE scraper for NYC 1980s Tax Department Photographs.
Uses Preservica REST API for fast downloads after browser authentication.

Strategy:
1. Selenium: One-time browser auth to get Preservica-Access-Token
2. httpx: Fast API calls using the token (~5-10 req/sec)
3. Target: 455k images in ~24 hours

API Endpoints Used:
- /api/content/search - Find objects by BBL metadata
- /api/content/thumbnail - Download thumbnail images

Usage:
    python scripts/scrape_tax_photos_turbo.py --limit 1000 --show-browser
    python scripts/scrape_tax_photos_turbo.py --test-token  # Test token extraction
"""

import os
import sys
import re
import argparse
import time
import random
import asyncio
import psycopg2
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from io import BytesIO
from PIL import Image
from tqdm import tqdm

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

from models.config import get_settings

# Selenium imports
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

# Preservica endpoints
PRESERVICA_BASE = "https://nycrecords.preservica.com"
PRESERVICA_ACCESS = "https://nycrecords.access.preservica.com"
API_BASE = f"{PRESERVICA_BASE}/api/content"

# Borough collection IDs for searching
BOROUGH_COLLECTIONS = {
    1: "SO_975f712b-36ad-47b7-9cbe-cc3903b25a28",  # Manhattan
    2: "SO_61d09827-787f-453c-a003-b1ccc2017c9e",  # Bronx
    3: "SO_fdf29daa-2b53-4fd9-b1b7-1280b7a02b8a",  # Brooklyn
    4: "SO_0fc6d8aa-5ed8-4da7-986a-8804668b3c10",  # Queens
    5: None,  # Staten Island - may not be available
}

# Local storage
LOCAL_STORAGE_BASE = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/tax_photos_1980s"

# Rate limiting (requests per second)
# 10 req/sec = 36k/hour = 864k/day = ~25 hours for 900k images
REQUESTS_PER_SECOND = 10


def create_driver(headless: bool = False):
    """Create Chrome driver for initial authentication."""
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.page_load_strategy = 'eager'
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1200,800")
    return uc.Chrome(options=options, use_subprocess=True)


def get_preservica_token(driver) -> Optional[str]:
    """
    Extract Preservica-Access-Token from browser after authentication.
    The token is typically stored in localStorage or as a cookie.
    """
    try:
        # Method 1: Check localStorage
        token = driver.execute_script("""
            return localStorage.getItem('preservica-access-token') ||
                   localStorage.getItem('accessToken') ||
                   localStorage.getItem('token');
        """)
        if token:
            return token

        # Method 2: Check sessionStorage
        token = driver.execute_script("""
            return sessionStorage.getItem('preservica-access-token') ||
                   sessionStorage.getItem('accessToken') ||
                   sessionStorage.getItem('token');
        """)
        if token:
            return token

        # Method 3: Look in cookies
        cookies = driver.get_cookies()
        for cookie in cookies:
            if 'token' in cookie['name'].lower() or 'access' in cookie['name'].lower():
                return cookie['value']

        # Method 4: Intercept network requests (check for token in headers)
        # This requires navigating to a page that makes API calls

        return None

    except Exception as e:
        print(f"Error extracting token: {e}")
        return None


def get_session_from_browser(driver) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Get cookies and headers from authenticated browser session.
    """
    cookies = {}
    for cookie in driver.get_cookies():
        cookies[cookie['name']] = cookie['value']

    # Get user agent from browser
    user_agent = driver.execute_script("return navigator.userAgent")

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Referer": PRESERVICA_ACCESS,
    }

    return cookies, headers


async def search_by_bbl(
    client: httpx.AsyncClient,
    borough: int,
    block: int,
    lot: int,
    headers: Dict[str, str]
) -> Optional[str]:
    """
    Search Preservica for a specific BBL and return the object ID.
    """
    try:
        # Build search query with BBL metadata
        params = {
            "q": "*",
            "metadata": f"oai_dc.coverage_block:{block} AND oai_dc.coverage_lot:{lot}",
            "start": 0,
            "max": 1,
        }

        # Add parent collection filter if available
        collection_id = BOROUGH_COLLECTIONS.get(borough)
        if collection_id:
            params["parenthierarchy"] = collection_id

        response = await client.get(
            f"{API_BASE}/search",
            params=params,
            headers=headers,
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("success") and data.get("value", {}).get("objectIds"):
                return data["value"]["objectIds"][0]

        return None

    except Exception as e:
        return None


async def download_thumbnail(
    client: httpx.AsyncClient,
    object_id: str,
    headers: Dict[str, str]
) -> Optional[bytes]:
    """
    Download thumbnail image for a given object ID.
    """
    try:
        response = await client.get(
            f"{API_BASE}/thumbnail",
            params={"id": object_id},
            headers=headers,
        )

        if response.status_code == 200 and len(response.content) > 1000:
            return response.content

        return None

    except Exception as e:
        return None


def save_image(image_bytes: bytes, borough: int, block: int, lot: int, output_dir: Path) -> str:
    """Save image locally."""
    try:
        img = Image.open(BytesIO(image_bytes))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')

        output = BytesIO()
        img.save(output, format='JPEG', quality=85, optimize=True)
        output.seek(0)
        image_bytes = output.read()
    except Exception as e:
        raise ValueError(f"Invalid image: {e}")

    save_dir = output_dir / str(borough) / str(block)
    save_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{borough}_{block}_{lot}.jpg"
    filepath = save_dir / filename

    with open(filepath, 'wb') as f:
        f.write(image_bytes)

    return str(filepath)


async def process_batch(
    client: httpx.AsyncClient,
    buildings: List[Tuple],
    headers: Dict[str, str],
    output_dir: Path,
    semaphore: asyncio.Semaphore,
    dry_run: bool = False
) -> Tuple[int, int, int]:
    """Process a batch of buildings concurrently."""
    success = 0
    skip = 0
    fail = 0

    async def process_one(bbl: str, bin_id: str):
        nonlocal success, skip, fail

        async with semaphore:
            try:
                # Parse BBL
                bbl_clean = bbl.replace("-", "").replace(",", "")
                if len(bbl_clean) >= 10:
                    borough = int(bbl_clean[0])
                    block = int(bbl_clean[1:6])
                    lot = int(bbl_clean[6:10])
                else:
                    fail += 1
                    return

                # Check if already exists
                save_dir = output_dir / str(borough) / str(block)
                filename = f"{borough}_{block}_{lot}.jpg"
                filepath = save_dir / filename

                if filepath.exists():
                    skip += 1
                    return

                if dry_run:
                    success += 1
                    return

                # Search for object
                object_id = await search_by_bbl(client, borough, block, lot, headers)
                if not object_id:
                    fail += 1
                    return

                # Download thumbnail
                image_bytes = await download_thumbnail(client, object_id, headers)
                if not image_bytes:
                    fail += 1
                    return

                # Save
                save_image(image_bytes, borough, block, lot, output_dir)
                success += 1

            except Exception as e:
                fail += 1

            # Small delay to respect rate limits
            await asyncio.sleep(1.0 / REQUESTS_PER_SECOND)

    tasks = [process_one(row[0], row[1]) for row in buildings]
    await asyncio.gather(*tasks)

    return success, skip, fail


async def run_turbo_mode(
    cookies: Dict[str, str],
    headers: Dict[str, str],
    token: Optional[str],
    output_dir: Path,
    limit: int,
    offset: int,
    dry_run: bool,
    batch_size: int = 100
):
    """Run the turbo mode scraper using httpx."""
    settings = get_settings()

    # Add token to headers if we have it
    if token:
        headers["Preservica-Access-Token"] = token

    print("\n📊 Connecting to database...")
    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT bbl, bin, building_name, address
        FROM buildings_full_merge_scanning
        WHERE bbl IS NOT NULL
          AND bbl != ''
          AND LENGTH(bbl) >= 10
        ORDER BY bbl
        OFFSET %s
        LIMIT %s
    """, (offset, limit))

    buildings = cur.fetchall()
    total = len(buildings)
    print(f"📊 Found {total} buildings with BBL")

    if dry_run:
        print("🔍 DRY RUN MODE")

    # Concurrency control
    semaphore = asyncio.Semaphore(REQUESTS_PER_SECOND)

    total_success = 0
    total_skip = 0
    total_fail = 0

    async with httpx.AsyncClient(
        cookies=cookies,
        timeout=30.0,
        follow_redirects=True
    ) as client:

        # Process in batches
        for i in range(0, total, batch_size):
            batch = buildings[i:i + batch_size]
            print(f"\n📦 Processing batch {i // batch_size + 1}/{(total + batch_size - 1) // batch_size}")

            success, skip, fail = await process_batch(
                client, batch, headers, output_dir, semaphore, dry_run
            )

            total_success += success
            total_skip += skip
            total_fail += fail

            print(f"   ✅ {success} downloaded, ⏭️ {skip} skipped, ❌ {fail} failed")
            print(f"   📈 Total: {total_success + total_skip + total_fail}/{total}")

    conn.close()

    print("\n" + "=" * 60)
    print("✅ TURBO MODE COMPLETE!")
    print("=" * 60)
    print(f"📊 Results:")
    print(f"   ✅ Downloaded: {total_success}")
    print(f"   ⏭️ Already had: {total_skip}")
    print(f"   ❌ Not found: {total_fail}")
    print(f"   📁 Output dir: {output_dir}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="TURBO MODE Tax Photo Scraper")
    parser.add_argument("--limit", type=int, default=1000, help="Max buildings")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N")
    parser.add_argument("--dry-run", action="store_true", help="Don't download")
    parser.add_argument("--show-browser", action="store_true", help="Show browser")
    parser.add_argument("--test-token", action="store_true", help="Test token extraction only")
    parser.add_argument("--output-dir", type=str, default=str(LOCAL_STORAGE_BASE))
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("🚀 Starting TURBO MODE Tax Photo Scraper")
    print(f"   📂 Output: {output_dir}")
    print(f"   ⚡ Rate: {REQUESTS_PER_SECOND} req/sec")

    # Phase 1: Browser authentication
    print("\n🌐 Phase 1: Browser Authentication")
    headless = not args.show_browser
    driver = create_driver(headless=headless)

    try:
        print(f"📍 Navigating to {PRESERVICA_ACCESS}...")
        driver.get(PRESERVICA_ACCESS)

        print("\n" + "!" * 60)
        print("MANUAL VERIFICATION REQUIRED")
        print("1. Check the browser window")
        print("2. Navigate to a tax photo collection page")
        print("3. Make sure images are loading")
        print("4. Press ENTER to extract session")
        print("!" * 60 + "\n")
        input("Press ENTER to continue...")

        # Extract session
        print("🔑 Extracting session...")
        token = get_preservica_token(driver)
        cookies, headers = get_session_from_browser(driver)

        if token:
            print(f"✅ Got token: {token[:20]}...")
        else:
            print("⚠️ No token found (will try with cookies only)")

        print(f"🍪 Got {len(cookies)} cookies")

        if args.test_token:
            print("\n🧪 Testing API access...")
            import requests
            test_headers = headers.copy()
            if token:
                test_headers["Preservica-Access-Token"] = token

            # Test search endpoint
            resp = requests.get(
                f"{API_BASE}/search",
                params={"q": "*", "max": 1},
                headers=test_headers,
                cookies=cookies
            )
            print(f"   Search API: {resp.status_code}")
            if resp.status_code == 200:
                print(f"   Response: {resp.text[:200]}...")
            else:
                print(f"   Error: {resp.text[:200]}...")
            return

        # Close browser - no longer needed
        driver.quit()
        print("✅ Browser closed, switching to TURBO MODE")

        # Phase 2: Fast API requests
        print("\n⚡ Phase 2: TURBO MODE")
        asyncio.run(run_turbo_mode(
            cookies=cookies,
            headers=headers,
            token=token,
            output_dir=output_dir,
            limit=args.limit,
            offset=args.offset,
            dry_run=args.dry_run,
            batch_size=args.batch_size
        ))

    except KeyboardInterrupt:
        print("\n⚠️ Interrupted by user")
    finally:
        try:
            driver.quit()
        except:
            pass


if __name__ == "__main__":
    main()
