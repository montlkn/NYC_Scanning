#!/usr/bin/env python3
"""
FULL 900K Tax Photo Scraper - Maximum Speed Edition

Two phases:
1. BUILD MAPPING: Scrape all 900k BBL->ObjectID mappings from Preservica
2. DOWNLOAD: Parallel download all images using the mapping

Speed: 20 req/sec = 72k/hour = ~12.5 hours for 900k images

For multi-machine setup:
  Machine 1: python scrape_tax_photos_full.py --boroughs 1,2,3
  Machine 2: python scrape_tax_photos_full.py --boroughs 4,5

Usage:
    # Build mapping first (required once)
    python scrape_tax_photos_full.py --build-mapping --show-browser

    # Then download all
    python scrape_tax_photos_full.py --download --workers 5

    # Or do both
    python scrape_tax_photos_full.py --build-mapping --download --show-browser

    # Multi-machine (split by borough)
    python scrape_tax_photos_full.py --download --boroughs 1,2,3
"""

import os
import sys
import json
import csv
import argparse
import time
import random
import asyncio
import aiofiles
import signal
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Set
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime
import httpx
from PIL import Image
from tqdm.asyncio import tqdm as async_tqdm
from tqdm import tqdm

# Global flag for graceful shutdown
SHUTDOWN_REQUESTED = False

def signal_handler(signum, frame):
    global SHUTDOWN_REQUESTED
    print("\n\n⚠️  Shutdown requested! Saving progress and exiting gracefully...")
    SHUTDOWN_REQUESTED = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

# Selenium imports
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ============================================================================
# CONFIGURATION
# ============================================================================

PRESERVICA_BASE = "https://nycrecords.preservica.com"
PRESERVICA_ACCESS = "https://nycrecords.access.preservica.com"
API_BASE = f"{PRESERVICA_BASE}/api/content"

# Borough collection IDs (1980s Tax Photos)
BOROUGH_COLLECTIONS = {
    1: ("Manhattan", "SO_975f712b-36ad-47b7-9cbe-cc3903b25a28"),
    2: ("Bronx", "SO_61d09827-787f-453c-a003-b1ccc2017c9e"),
    3: ("Brooklyn", "SO_fdf29daa-2b53-4fd9-b1b7-1280b7a02b8a"),
    4: ("Queens", "SO_0fc6d8aa-5ed8-4da7-986a-8804668b3c10"),
    5: ("Staten Island", "SO_d1a15702-bc15-474b-9663-c1820d5ae2e3"),
}

# Storage paths - organized by BIN for easy R2 migration
LOCAL_STORAGE_BASE = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Tax_Lot_Images"
MAPPING_FILE = Path(__file__).parent / "tax_photos_mapping_full.csv"
PROGRESS_FILE = Path(__file__).parent / "tax_photos_progress.json"

# Rate limiting - CONFIGURABLE
DEFAULT_REQUESTS_PER_SECOND = 30  # 30/sec = ~8 hours for 900k (safe for gov archive)
DEFAULT_WORKERS = 10  # Parallel download workers
BATCH_SIZE = 100  # Items per API page request

# Database imports
import psycopg2


# ============================================================================
# BROWSER AUTHENTICATION
# ============================================================================

def create_driver(headless: bool = False):
    """Create Chrome driver for authentication."""
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.page_load_strategy = 'eager'
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1200,800")
    return uc.Chrome(options=options, use_subprocess=True)


def get_session_from_browser(driver) -> Tuple[Dict[str, str], Dict[str, str], Optional[str]]:
    """Extract cookies, headers, and token from authenticated browser."""
    cookies = {}
    for cookie in driver.get_cookies():
        cookies[cookie['name']] = cookie['value']

    user_agent = driver.execute_script("return navigator.userAgent")

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Referer": PRESERVICA_ACCESS,
        "Origin": PRESERVICA_ACCESS,
    }

    # Try to extract token
    token = None
    for storage in ['localStorage', 'sessionStorage']:
        for key in ['preservica-access-token', 'accessToken', 'token', 'access_token']:
            try:
                val = driver.execute_script(f"return {storage}.getItem('{key}')")
                if val:
                    token = val
                    break
            except:
                pass
        if token:
            break

    return cookies, headers, token


def authenticate(show_browser: bool = True) -> Tuple[Dict, Dict, Optional[str]]:
    """Authenticate with Preservica and return session info."""
    print("\n🌐 Opening browser for authentication...")
    driver = create_driver(headless=not show_browser)

    try:
        driver.get(PRESERVICA_ACCESS)
        time.sleep(3)

        # Navigate to a collection to trigger any auth
        driver.get(f"{PRESERVICA_ACCESS}/uncategorized/{BOROUGH_COLLECTIONS[1][1]}/")
        time.sleep(2)

        print("\n" + "=" * 60)
        print("AUTHENTICATION REQUIRED")
        print("=" * 60)
        print("1. Browser should show NYC Municipal Archives")
        print("2. If there's a Cloudflare challenge, solve it")
        print("3. Make sure you can see the photo thumbnails")
        print("4. Press ENTER when ready")
        print("=" * 60)
        input("\nPress ENTER to continue...")

        cookies, headers, token = get_session_from_browser(driver)
        print(f"\n✅ Got {len(cookies)} cookies")
        if token:
            print(f"✅ Got token: {token[:30]}...")

        return cookies, headers, token

    finally:
        driver.quit()
        print("✅ Browser closed")


# ============================================================================
# DATABASE: Get buildings from Supabase
# ============================================================================

def get_buildings_from_db(boroughs: List[int], limit: Optional[int] = None, offset: int = 0) -> List[Dict]:
    """
    Get buildings from Supabase with BIN, BBL, address.
    We use these to search Preservica and save by BIN.
    """
    from models.config import get_settings
    settings = get_settings()

    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()

    # Build borough filter from BBL (first digit = borough)
    borough_conditions = " OR ".join([f"bbl LIKE '{b}%'" for b in boroughs])

    query = f"""
        SELECT DISTINCT bin, bbl, address, building_name
        FROM buildings_full_merge_scanning
        WHERE bbl IS NOT NULL
          AND bbl != ''
          AND LENGTH(bbl) >= 10
          AND ({borough_conditions})
        ORDER BY bbl
        OFFSET %s
    """

    if limit:
        query += f" LIMIT {limit}"

    cur.execute(query, (offset,))
    rows = cur.fetchall()
    conn.close()

    buildings = []
    for row in rows:
        bin_id, bbl, address, name = row
        # Parse BBL to get borough, block, lot
        bbl_clean = str(bbl).replace("-", "").replace(" ", "")
        if len(bbl_clean) >= 10:
            borough = int(bbl_clean[0])
            block = int(bbl_clean[1:6])
            lot = int(bbl_clean[6:10])
            buildings.append({
                'bin': str(bin_id).replace('.0', ''),
                'bbl': bbl,
                'borough': borough,
                'block': block,
                'lot': lot,
                'address': address or '',
                'name': name or ''
            })

    return buildings


# ============================================================================
# PRESERVICA API: Search and Download
# ============================================================================

async def search_preservica_by_bbl(
    client: httpx.AsyncClient,
    borough: int,
    block: int,
    lot: int,
    headers: Dict,
    semaphore: asyncio.Semaphore
) -> Optional[str]:
    """Search Preservica for a tax photo by BBL, return object ID."""
    async with semaphore:
        try:
            collection_id = BOROUGH_COLLECTIONS.get(borough, (None, None))[1]
            if not collection_id:
                return None

            # Search by block and lot metadata
            params = {
                "q": "*",
                "metadata": f"oai_dc.coverage_block:{block} AND oai_dc.coverage_lot:{lot}",
                "parenthierarchy": collection_id,
                "start": 0,
                "max": 1,
            }

            response = await client.get(
                f"{API_BASE}/search",
                params=params,
                headers=headers,
                timeout=30.0
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    object_ids = data.get("value", {}).get("objectIds", [])
                    if object_ids:
                        return object_ids[0]

            # If metadata search fails, try title search (dof_BOROUGH_BLOCK_LOT)
            title_query = f"dof_{borough}_{block:05d}_{lot:04d}"
            params = {
                "q": title_query,
                "parenthierarchy": collection_id,
                "start": 0,
                "max": 1,
            }

            response = await client.get(
                f"{API_BASE}/search",
                params=params,
                headers=headers,
                timeout=30.0
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    object_ids = data.get("value", {}).get("objectIds", [])
                    if object_ids:
                        return object_ids[0]

            return None

        except Exception as e:
            return None

        finally:
            await asyncio.sleep(1.0 / DEFAULT_REQUESTS_PER_SECOND)


def load_progress() -> Set[str]:
    """Load set of already-downloaded object IDs from progress file AND existing files."""
    downloaded = set()

    # Load from progress file
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, 'r') as f:
                data = json.load(f)
                downloaded = set(data.get('downloaded', []))
                print(f"   📄 Loaded {len(downloaded):,} from progress file")
        except:
            pass

    # Also scan output directory for existing files (in case progress file was lost)
    if LOCAL_STORAGE_BASE.exists():
        for borough_dir in LOCAL_STORAGE_BASE.iterdir():
            if borough_dir.is_dir():
                for img_file in borough_dir.glob("*.jpg"):
                    # Extract object_id from filename
                    obj_id = img_file.stem.replace("_", "|").replace("_", ":")
                    downloaded.add(obj_id)
        print(f"   📁 Found {len(downloaded):,} existing files total")

    return downloaded


def save_progress(downloaded: Set[str]):
    """Save progress to file (atomic write)."""
    # Write to temp file first, then rename (atomic)
    temp_file = PROGRESS_FILE.with_suffix('.tmp')
    with open(temp_file, 'w') as f:
        json.dump({
            'downloaded': list(downloaded),
            'count': len(downloaded),
            'last_updated': datetime.now().isoformat()
        }, f)
    temp_file.rename(PROGRESS_FILE)


async def download_image(
    client: httpx.AsyncClient,
    object_id: str,
    borough: int,
    headers: Dict,
    output_dir: Path,
    semaphore: asyncio.Semaphore
) -> Optional[str]:
    """Download a single image."""
    async with semaphore:
        try:
            # Get thumbnail
            response = await client.get(
                f"{API_BASE}/thumbnail",
                params={"id": object_id},
                headers=headers,
                timeout=30.0
            )

            if response.status_code != 200 or len(response.content) < 1000:
                return None

            # Save image
            save_dir = output_dir / str(borough)
            save_dir.mkdir(parents=True, exist_ok=True)

            # Use object_id as filename (we can rename later with BBL)
            safe_id = object_id.replace("|", "_").replace(":", "_")
            filepath = save_dir / f"{safe_id}.jpg"

            # Convert to JPEG
            try:
                img = Image.open(BytesIO(response.content))
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                img.save(filepath, format='JPEG', quality=85, optimize=True)
            except:
                # Save raw if can't process
                with open(filepath, 'wb') as f:
                    f.write(response.content)

            return str(filepath)

        except Exception as e:
            return None

        finally:
            await asyncio.sleep(1.0 / DEFAULT_REQUESTS_PER_SECOND)


async def download_worker(
    worker_id: int,
    queue: asyncio.Queue,
    client: httpx.AsyncClient,
    headers: Dict,
    output_dir: Path,
    semaphore: asyncio.Semaphore,
    progress: Set[str],
    progress_lock: asyncio.Lock,
    stats: Dict
):
    """Worker that downloads images from the queue."""
    global SHUTDOWN_REQUESTED

    while not SHUTDOWN_REQUESTED:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=2.0)
        except asyncio.TimeoutError:
            if queue.empty():
                break
            continue

        borough, object_id = item

        # Skip if already downloaded
        if object_id in progress:
            stats['skipped'] += 1
            queue.task_done()
            continue

        result = await download_image(
            client, object_id, borough, headers, output_dir, semaphore
        )

        async with progress_lock:
            if result:
                progress.add(object_id)
                stats['success'] += 1
            else:
                stats['failed'] += 1

            # Save progress every 500 items (more frequent for safety)
            if (stats['success'] + stats['failed']) % 500 == 0:
                save_progress(progress)

        queue.task_done()

    # Final save when worker exits
    async with progress_lock:
        save_progress(progress)


async def download_all(
    cookies: Dict,
    headers: Dict,
    token: Optional[str],
    boroughs: List[int],
    output_dir: Path,
    num_workers: int,
    rate_limit: int
):
    """Download all images using parallel workers."""
    print("\n" + "=" * 60)
    print("PHASE 2: DOWNLOADING IMAGES")
    print("=" * 60)

    if token:
        headers["Preservica-Access-Token"] = token

    # Load mapping
    items = load_mapping(boroughs)
    if not items:
        return

    print(f"📊 Total items to process: {len(items):,}")

    # Load progress
    progress = load_progress()
    print(f"⏭️  Already downloaded: {len(progress):,}")

    remaining = [(b, oid) for b, oid in items if oid not in progress]
    print(f"📥 Remaining to download: {len(remaining):,}")

    if not remaining:
        print("✅ All items already downloaded!")
        return

    # Setup
    output_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(rate_limit)
    queue = asyncio.Queue()
    progress_lock = asyncio.Lock()
    stats = {'success': 0, 'failed': 0, 'skipped': len(progress)}

    # Fill queue
    for item in remaining:
        await queue.put(item)

    print(f"\n⚡ Starting {num_workers} workers at {rate_limit} req/sec...")
    print(f"   Estimated time: {len(remaining) / rate_limit / 3600:.1f} hours")

    async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as client:
        # Create workers
        workers = [
            asyncio.create_task(
                download_worker(
                    i, queue, client, headers, output_dir,
                    semaphore, progress, progress_lock, stats
                )
            )
            for i in range(num_workers)
        ]

        # Progress bar
        with tqdm(total=len(remaining), desc="Downloading", unit="img") as pbar:
            last_count = 0
            while not queue.empty() or any(not w.done() for w in workers):
                await asyncio.sleep(1)
                current = stats['success'] + stats['failed']
                pbar.update(current - last_count)
                last_count = current
                pbar.set_postfix({
                    'ok': stats['success'],
                    'fail': stats['failed'],
                    'skip': stats['skipped']
                })

        # Wait for workers to finish
        await asyncio.gather(*workers, return_exceptions=True)

    # Final progress save
    save_progress(progress)

    print("\n" + "=" * 60)
    print("✅ DOWNLOAD COMPLETE!")
    print("=" * 60)
    print(f"📊 Results:")
    print(f"   ✅ Downloaded: {stats['success']:,}")
    print(f"   ⏭️  Skipped: {stats['skipped']:,}")
    print(f"   ❌ Failed: {stats['failed']:,}")
    print(f"   📁 Output: {output_dir}")
    print("=" * 60)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Full 900K Tax Photo Scraper")
    parser.add_argument("--build-mapping", action="store_true", help="Build BBL->ObjectID mapping")
    parser.add_argument("--download", action="store_true", help="Download images")
    parser.add_argument("--show-browser", action="store_true", help="Show browser for auth")
    parser.add_argument("--boroughs", type=str, default="1,2,3,4,5", help="Boroughs to process (comma-separated)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Number of parallel workers")
    parser.add_argument("--rate", type=int, default=DEFAULT_REQUESTS_PER_SECOND, help="Requests per second")
    parser.add_argument("--output-dir", type=str, default=str(LOCAL_STORAGE_BASE), help="Output directory")
    args = parser.parse_args()

    if not args.build_mapping and not args.download:
        print("❌ Specify --build-mapping and/or --download")
        parser.print_help()
        return

    # Parse boroughs
    boroughs = [int(b.strip()) for b in args.boroughs.split(",")]
    print(f"📍 Processing boroughs: {boroughs}")

    # Authenticate
    cookies, headers, token = authenticate(show_browser=args.show_browser)

    output_dir = Path(args.output_dir)

    # Phase 1: Build mapping
    if args.build_mapping:
        asyncio.run(build_full_mapping(cookies, headers, token, boroughs))

    # Phase 2: Download
    if args.download:
        asyncio.run(download_all(
            cookies, headers, token, boroughs,
            output_dir, args.workers, args.rate
        ))


if __name__ == "__main__":
    main()
