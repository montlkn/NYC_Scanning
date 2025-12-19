#!/usr/bin/env python3
"""
Selenium-based scraper for NYC 1980s Tax Department Photographs.
Uses BBL (Borough-Block-Lot) for exact matching - no duplicates!

Uses "Smart Waiting" + Single Browser Session (same pattern as LPC scraper).
Bypasses Cloudflare via manual initial verification.

455k+ images from 1982-1987 comprehensive citywide survey.
Perfect for CLIP embeddings due to consistent standardized angles.

Saves images LOCALLY to iCloud Drive:
    {LOCAL_STORAGE_DIR}/{borough}/{block}/{borough}_{block}_{lot}.jpg

Usage:
    python scripts/scrape_tax_photos.py --limit 100 --show-browser
    python scripts/scrape_tax_photos.py --limit 100 --dry-run
    python scripts/scrape_tax_photos.py --bbl "1-1234-56"  # Manhattan block 1234, lot 56

Requirements:
    pip install selenium undetected-chromedriver pillow psycopg2-binary tqdm
"""

import os
import sys
import re
import argparse
import time
import random
import psycopg2
import requests
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from io import BytesIO
from PIL import Image
from tqdm import tqdm

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Load environment variables BEFORE importing config
from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

from models.config import get_settings

# Selenium imports
import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Preservica base URL
PRESERVICA_BASE = "https://nycrecords.access.preservica.com"

# Borough codes and collection IDs
BOROUGH_COLLECTIONS = {
    1: ("Manhattan", "SO_975f712b-36ad-47b7-9cbe-cc3903b25a28"),
    2: ("Bronx", "SO_61d09827-787f-453c-a003-b1ccc2017c9e"),
    3: ("Brooklyn", "SO_fdf29daa-2b53-4fd9-b1b7-1280b7a02b8a"),
    4: ("Queens", "SO_0fc6d8aa-5ed8-4da7-986a-8804668b3c10"),
    5: ("Staten Island", None),  # May not be available
}

# Local storage for downloaded images
LOCAL_STORAGE_BASE = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/tax_photos_1980s"


def create_driver(headless: bool = True):
    """Create undetected Chrome WebDriver to bypass Cloudflare."""
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")

    # Speed Optimization: Eager load strategy (don't wait for all assets)
    options.page_load_strategy = 'eager'

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1200,800")

    driver = uc.Chrome(options=options, use_subprocess=True)
    return driver


def parse_bbl(bbl_str: str) -> Tuple[int, int, int]:
    """Parse BBL string into (borough, block, lot) tuple."""
    # Handle various formats: "1-1234-56", "1012340056", "1,1234,56"
    bbl_str = bbl_str.replace("-", "").replace(",", "").replace(" ", "")

    if len(bbl_str) == 10:
        # Standard 10-digit BBL format
        borough = int(bbl_str[0])
        block = int(bbl_str[1:6])
        lot = int(bbl_str[6:10])
    else:
        # Try splitting by common delimiters
        parts = re.split(r'[-,\s]+', bbl_str.strip())
        if len(parts) == 3:
            borough, block, lot = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            raise ValueError(f"Cannot parse BBL: {bbl_str}")

    return borough, block, lot


def get_search_url(borough: int, block: int, lot: int) -> Optional[str]:
    """Build Preservica search URL for a specific BBL."""
    if borough not in BOROUGH_COLLECTIONS:
        return None

    borough_name, collection_id = BOROUGH_COLLECTIONS[borough]
    if not collection_id:
        return None

    # Format: /uncategorized/{collection}/?hh_cmis_filter=oai_dc.coverage_block/{block}|oai_dc.coverage_lot/{lot}
    return f"{PRESERVICA_BASE}/uncategorized/{collection_id}/?hh_cmis_filter=oai_dc.coverage_block/{block}|oai_dc.coverage_lot/{lot}"


def find_image_on_page(driver, verbose: bool = False) -> Optional[str]:
    """Find the tax photo image URL on the current page."""
    try:
        # Wait for page to load
        time.sleep(4)

        # Debug: Print page info
        if verbose:
            print(f"    📄 Page title: {driver.title}")
            print(f"    🔗 URL: {driver.current_url[:80]}...")

        # Strategy 1: Look for clickable items in search results
        item_selectors = [
            ".archive-tile a",
            ".archive-item a",
            "a.item-link",
            ".thumbnail-link",
            ".result-item a",
            "a[href*='IO_']",  # Preservica Information Object links
        ]

        for selector in item_selectors:
            try:
                items = driver.find_elements(By.CSS_SELECTOR, selector)
                if items:
                    if verbose:
                        print(f"    🔍 Found {len(items)} items with selector: {selector}")
                    # Click first item to go to detail page
                    items[0].click()
                    time.sleep(3)
                    break
            except:
                continue

        # Strategy 2: Look for image viewer or main image
        img_selectors = [
            "img.main-image",
            "img.asset-image",
            "#imageViewer img",
            ".image-viewer img",
            "img[src*='Render']",
            "img[src*='api/entity']",
            ".pca-image img",
            "img[alt*='photograph']",
            "img[alt*='photo']",
        ]

        for selector in img_selectors:
            try:
                imgs = driver.find_elements(By.CSS_SELECTOR, selector)
                for img in imgs:
                    src = img.get_attribute("src")
                    if src and len(src) > 50:  # Skip small placeholder URLs
                        if verbose:
                            print(f"    ✅ Found via selector '{selector}': {src[:60]}...")
                        return src
            except:
                continue

        # Strategy 3: Look for any substantial image
        all_imgs = driver.find_elements(By.TAG_NAME, "img")
        if verbose:
            print(f"    🔍 Total images on page: {len(all_imgs)}")

        for img in all_imgs:
            src = img.get_attribute("src") or ""
            # Skip logos, icons, placeholders
            skip_patterns = ["logo", "icon", "favicon", "powered-by", "wp-content", "avatar", "placeholder"]
            if any(skip in src.lower() for skip in skip_patterns):
                continue

            # Look for actual content images
            if any(pattern in src.lower() for pattern in ["render", "api/entity", "media", "content", "asset"]):
                if verbose:
                    print(f"    ✅ Found content image: {src[:60]}...")
                return src

            # Check image dimensions - real photos are bigger
            try:
                width = img.get_attribute("width") or img.get_attribute("naturalWidth")
                height = img.get_attribute("height") or img.get_attribute("naturalHeight")
                if width and height:
                    w, h = int(width), int(height)
                    if w > 200 and h > 200:
                        if verbose:
                            print(f"    ✅ Found large image ({w}x{h}): {src[:60]}...")
                        return src
            except:
                pass

        if verbose:
            # Debug: Show all image sources
            print("    ⚠️ No suitable image found. All img sources:")
            for img in all_imgs[:10]:
                src = img.get_attribute("src") or "(no src)"
                print(f"       - {src[:80]}")

        return None

    except Exception as e:
        print(f"    ❌ Error finding image: {e}")
        return None


def download_image(url: str, session: requests.Session = None) -> Optional[bytes]:
    """Download image from URL."""
    try:
        if session:
            response = session.get(url, timeout=30)
        else:
            response = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
            })

        if response.status_code == 200 and len(response.content) > 5000:
            return response.content
        else:
            return None
    except Exception as e:
        print(f"    ❌ Download error: {e}")
        return None


def save_image(image_bytes: bytes, bbl: str, output_dir: Path) -> str:
    """Save image locally, organized by BBL."""
    # Validate and convert to JPEG
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

    # Create directory structure: tax_photos_1980s/{borough}/{block}/
    borough, block, lot = parse_bbl(bbl)
    save_dir = output_dir / str(borough) / str(block)
    save_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{borough}_{block}_{lot}.jpg"
    filepath = save_dir / filename

    with open(filepath, 'wb') as f:
        f.write(image_bytes)

    return str(filepath)


def process_bbl(
    driver,
    bbl: str,
    output_dir: Path,
    dry_run: bool = False,
    verbose: bool = True
) -> Tuple[bool, Optional[str]]:
    """
    Process a single BBL: navigate to Preservica, find image, download.

    Returns (success, filepath)
    """
    try:
        borough, block, lot = parse_bbl(bbl)
    except ValueError as e:
        if verbose:
            print(f"    ❌ Invalid BBL: {e}")
        return (False, None)

    # Check if already downloaded
    save_dir = output_dir / str(borough) / str(block)
    filename = f"{borough}_{block}_{lot}.jpg"
    filepath = save_dir / filename

    if filepath.exists():
        if verbose:
            print(f"    ⏭️ Already exists: {filepath}")
        return (True, str(filepath))

    # Get search URL
    search_url = get_search_url(borough, block, lot)
    if not search_url:
        if verbose:
            print(f"    ❌ No collection for borough {borough}")
        return (False, None)

    if verbose:
        print(f"    🔍 Searching: {BOROUGH_COLLECTIONS[borough][0]} Block {block} Lot {lot}")

    try:
        # Navigate to search results
        driver.get(search_url)
        time.sleep(4)  # Wait for page load

        # Find image URL
        image_url = find_image_on_page(driver, verbose=verbose)

        if not image_url:
            if verbose:
                print(f"    ⚠️ No image found")
            return (False, None)

        if verbose:
            print(f"    ✅ Found image: {image_url[:60]}...")

        if dry_run:
            return (True, None)

        # Download image
        image_bytes = download_image(image_url)
        if not image_bytes:
            return (False, None)

        # Save locally
        saved_path = save_image(image_bytes, bbl, output_dir)
        if verbose:
            print(f"    💾 Saved: {saved_path}")

        return (True, saved_path)

    except Exception as e:
        if verbose:
            print(f"    ❌ Error: {e}")
        return (False, None)


def main():
    parser = argparse.ArgumentParser(description="Scrape 1980s Tax Photos by BBL")
    parser.add_argument("--limit", type=int, default=100, help="Max buildings to process")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N buildings")
    parser.add_argument("--dry-run", action="store_true", help="Don't download images")
    parser.add_argument("--bbl", type=str, help="Process single BBL (e.g., '1-1234-56')")
    parser.add_argument("--show-browser", action="store_true", help="Show browser window")
    parser.add_argument("--output-dir", type=str, default=str(LOCAL_STORAGE_BASE), help="Output directory")
    args = parser.parse_args()

    headless = not args.show_browser
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()

    print("🏛️ Starting 1980s Tax Photo Scraper")
    print(f"   📂 Output: {output_dir}")
    print(f"   🖥️ Headless: {headless}")

    # Create driver
    print("🌐 Initializing Chrome WebDriver...")
    driver = create_driver(headless=headless)

    try:
        # Initial navigation to handle any Cloudflare challenges
        print(f"📍 Navigating to {PRESERVICA_BASE}...")
        driver.get(PRESERVICA_BASE)

        if not headless:
            print("\n" + "!" * 60)
            print("MANUAL VERIFICATION MAY BE REQUIRED")
            print("1. Check the browser window")
            print("2. Solve any Cloudflare challenges")
            print("3. Press ENTER to continue")
            print("!" * 60 + "\n")
            input("Press ENTER to continue...")
        else:
            time.sleep(5)  # Wait for any automatic challenges

        print("✅ Ready to scrape!")

        if args.bbl:
            # Single BBL mode
            print(f"\n🏠 Processing single BBL: {args.bbl}")
            success, filepath = process_bbl(
                driver, args.bbl, output_dir,
                dry_run=args.dry_run, verbose=True
            )
            if success:
                print(f"✅ Success!")
            else:
                print(f"❌ Failed")
            return

        # Batch mode - get BBLs from database
        print("\n📊 Connecting to database...")
        conn = psycopg2.connect(settings.database_url)
        cur = conn.cursor()

        # Query buildings with BBL
        cur.execute("""
            SELECT DISTINCT bbl, bin, building_name, address
            FROM buildings_full_merge_scanning
            WHERE bbl IS NOT NULL
              AND bbl != ''
              AND LENGTH(bbl) >= 10
            ORDER BY bbl
            OFFSET %s
            LIMIT %s
        """, (args.offset, args.limit))

        buildings = cur.fetchall()
        print(f"📊 Found {len(buildings)} buildings with BBL")

        if args.dry_run:
            print("🔍 DRY RUN MODE - No downloads")

        success_count = 0
        skip_count = 0
        fail_count = 0

        pbar = tqdm(buildings, desc="Scraping", unit="bldg")

        for row in pbar:
            bbl, bin_id, name, address = row

            # Update progress bar
            display_name = (name or address or bbl)[:30]
            pbar.set_description(f"Scraping {display_name}")

            success, filepath = process_bbl(
                driver, bbl, output_dir,
                dry_run=args.dry_run, verbose=False
            )

            if success:
                if filepath and "Already exists" not in str(filepath):
                    success_count += 1
                else:
                    skip_count += 1
            else:
                fail_count += 1

            # Rate limiting - matches LPC scraper pattern (Selenium is already slow/safe)
            delay = random.uniform(1.0, 2.0)
            time.sleep(delay)

        conn.close()

        print("\n" + "=" * 60)
        print("✅ COMPLETE!")
        print("=" * 60)
        print(f"📊 Results:")
        print(f"   ✅ Downloaded: {success_count}")
        print(f"   ⏭️ Already had: {skip_count}")
        print(f"   ❌ Not found: {fail_count}")
        print(f"   📁 Output dir: {output_dir}")

        if args.dry_run:
            print("\n🔍 DRY RUN - No images were downloaded")

        print("=" * 60)

    finally:
        driver.quit()
        print("\n🛑 Browser closed")


if __name__ == "__main__":
    main()
