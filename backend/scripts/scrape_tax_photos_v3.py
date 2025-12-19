#!/usr/bin/env python3
"""
Tax Photo Scraper V3 - Selenium Thumbnail Extraction

Strategy: Navigate search results pages with Selenium, extract thumbnail URLs
directly from the rendered page (browser handles auth automatically).

This is slower than API but more reliable since it uses the same auth as the browser.

Usage:
    python3 scripts/scrape_tax_photos_v3.py --show-browser --boroughs 1
    python3 scripts/scrape_tax_photos_v3.py --show-browser --test
"""

import os
import sys
import re
import json
import argparse
import time
import signal
import requests
from pathlib import Path
from typing import Optional, List, Dict, Set
from io import BytesIO
from datetime import datetime
from PIL import Image
from tqdm import tqdm

# Global shutdown flag
SHUTDOWN_REQUESTED = False

def signal_handler(signum, frame):
    global SHUTDOWN_REQUESTED
    print("\n\n>>> Shutdown requested! Saving progress...")
    SHUTDOWN_REQUESTED = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

import psycopg2
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ============================================================================
# CONFIGURATION
# ============================================================================

PRESERVICA_ACCESS = "https://nycrecords.access.preservica.com"

# Borough collection IDs
BOROUGH_COLLECTIONS = {
    1: ("Manhattan", "SO_975f712b-36ad-47b7-9cbe-cc3903b25a28"),
    2: ("Bronx", "SO_61d09827-787f-453c-a003-b1ccc2017c9e"),
    3: ("Brooklyn", "SO_fdf29daa-2b53-4fd9-b1b7-1280b7a02b8a"),
    4: ("Queens", "SO_0fc6d8aa-5ed8-4da7-986a-8804668b3c10"),
    5: ("Staten Island", "SO_d1a15702-bc15-474b-9663-c1820d5ae2e3"),
}

BOROUGH_NAMES = {
    1: "Manhattan",
    2: "Bronx",
    3: "Brooklyn",
    4: "Queens",
    5: "Staten_Island"
}

# Output directory
OUTPUT_BASE = Path("/Users/lucienmount/Library/Mobile Documents/com~apple~CloudDocs/tax_photos_1980s")

# Progress tracking
PROGRESS_FILE = Path(__file__).parent / "tax_photos_v3_progress.json"

# Rate limiting (per page, not per request)
PAGE_DELAY = 2.0  # seconds between pages


# ============================================================================
# HELPERS
# ============================================================================

def sanitize_for_filename(text: str) -> str:
    if not text: return ""
    return re.sub(r'[<>:"/\\|?*]', '', text).strip()[:50]

def parse_bbl_from_text(text: str) -> Optional[str]:
    """Extract BBL from text like 'dof_1_00001_0010'."""
    if not text: return None
    m = re.search(r'dof_(\d)_(\d{5})_(\d{4})', text, re.IGNORECASE)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    m = re.search(r'(\d)[-_](\d{5})[-_](\d{4})', text)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    return None

def get_save_path(borough: int, bbl: str, bin_id: Optional[str] = None) -> Path:
    borough_name = BOROUGH_NAMES.get(borough, str(borough))
    if bin_id:
        folder = bin_id
    else:
        folder = f"unmatched_{bbl}"
    save_dir = OUTPUT_BASE / borough_name / folder
    filename = f"tax_lot_image_1980_{bbl}.jpg"
    return save_dir / filename

def load_progress() -> Dict:
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {'downloaded_bbls': [], 'last_page': {}, 'last_updated': None}

def save_progress(progress: Dict):
    progress['last_updated'] = datetime.now().isoformat()
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)

def get_supabase_mapping() -> Dict[str, str]:
    """Load BBL -> BIN mapping from Supabase."""
    from models.config import get_settings
    settings = get_settings()

    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()

    print("\n[DB] Loading BBL->BIN mapping...")
    cur.execute("""
        SELECT bbl, bin
        FROM buildings_full_merge_scanning
        WHERE bbl IS NOT NULL AND bin IS NOT NULL
    """)
    rows = cur.fetchall()
    conn.close()

    mapping = {}
    for bbl, bin_id in rows:
        clean_bbl = str(bbl).replace(".0", "").replace("-", "").replace(" ", "")
        if len(clean_bbl) == 10:
            mapping[clean_bbl] = str(bin_id).replace(".0", "")

    print(f"[DB] Loaded {len(mapping):,} mappings")
    return mapping


# ============================================================================
# SELENIUM DRIVER
# ============================================================================

def create_driver(headless: bool = False):
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.page_load_strategy = 'eager'
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,900")
    return uc.Chrome(options=options, use_subprocess=True)


# ============================================================================
# SCRAPER LOGIC
# ============================================================================

def extract_thumbnails_from_page(driver) -> List[Dict]:
    """
    Extract all thumbnail info from current search results page.
    Returns list of {url, title, bbl}
    """
    thumbnails = []

    # Wait for thumbnails to load
    time.sleep(2)

    # Find all thumbnail containers
    # Preservica uses various class names, try multiple selectors
    selectors = [
        ".archive-tile",
        ".search-result-item",
        ".result-item",
        ".pca-tile",
        "[class*='tile']",
        "[class*='result']",
    ]

    items = []
    for sel in selectors:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, sel)
            if found:
                items = found
                break
        except:
            pass

    if not items:
        # Fallback: find all images that look like thumbnails
        imgs = driver.find_elements(By.TAG_NAME, "img")
        for img in imgs:
            src = img.get_attribute("src") or ""
            alt = img.get_attribute("alt") or ""
            title = img.get_attribute("title") or alt

            # Skip non-content images
            if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'wp-content']):
                continue

            # Look for Preservica content URLs
            if 'api/' in src or 'Render' in src or 'thumbnail' in src.lower():
                bbl = parse_bbl_from_text(title) or parse_bbl_from_text(alt)
                if bbl:
                    thumbnails.append({
                        'url': src,
                        'title': title,
                        'bbl': bbl
                    })
    else:
        # Extract from item containers
        for item in items:
            try:
                # Get title/link
                link = item.find_element(By.TAG_NAME, "a")
                title = link.get_attribute("title") or link.text

                # Get thumbnail image
                img = item.find_element(By.TAG_NAME, "img")
                src = img.get_attribute("src")

                if src and title:
                    bbl = parse_bbl_from_text(title)
                    if bbl:
                        thumbnails.append({
                            'url': src,
                            'title': title,
                            'bbl': bbl
                        })
            except:
                pass

    return thumbnails


def get_total_results(driver) -> int:
    """Try to extract total result count from page."""
    try:
        # Look for text like "1-50 of 150,000 results"
        text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r'of\s+([\d,]+)\s*result', text, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(',', ''))
    except:
        pass
    return 0


def download_and_save_image(url: str, save_path: Path, session: requests.Session) -> bool:
    """Download image and save to disk."""
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code != 200 or len(resp.content) < 1000:
            return False

        # Convert to JPEG
        img = Image.open(BytesIO(resp.content))
        if img.mode != 'RGB':
            img = img.convert('RGB')

        save_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(save_path, format='JPEG', quality=85)
        return True
    except Exception as e:
        return False


def run_scraper(driver, boroughs: List[int], bbl_map: Dict[str, str], dry_run: bool = False):
    """Main scraper loop."""
    print("\n" + "="*60)
    print("SELENIUM THUMBNAIL SCRAPER")
    print("="*60)

    # Load progress
    progress = load_progress()
    downloaded_bbls = set(progress.get('downloaded_bbls', []))
    print(f"[Progress] Already downloaded: {len(downloaded_bbls):,}")

    # Create session for downloading (uses browser cookies)
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie['name'], cookie['value'])
    session.headers['User-Agent'] = driver.execute_script("return navigator.userAgent")

    stats = {'success': 0, 'skip': 0, 'fail': 0, 'no_bbl': 0}

    for borough in boroughs:
        if SHUTDOWN_REQUESTED:
            break

        col_name, col_id = BOROUGH_COLLECTIONS[borough]
        print(f"\n[Borough] {col_name}")

        # Navigate to collection search
        base_url = f"{PRESERVICA_ACCESS}/uncategorized/{col_id}/"
        driver.get(base_url)
        time.sleep(3)

        # Get total results
        total = get_total_results(driver)
        print(f"[Search] Found approximately {total:,} items")

        # Resume from last page if available
        start_page = progress.get('last_page', {}).get(str(borough), 0)
        if start_page > 0:
            print(f"[Resume] Starting from page {start_page}")

        page = start_page
        items_per_page = 50  # Typical Preservica page size

        pbar = tqdm(desc=f"{col_name}", unit="img", total=total)
        pbar.update(page * items_per_page)

        while not SHUTDOWN_REQUESTED:
            # Navigate to page (if not first page)
            if page > 0:
                page_url = f"{base_url}?pg={page}"
                driver.get(page_url)
                time.sleep(PAGE_DELAY)

            # Extract thumbnails from current page
            thumbnails = extract_thumbnails_from_page(driver)

            if not thumbnails:
                print(f"\n[Page {page}] No thumbnails found, end of results")
                break

            # Process each thumbnail
            for thumb in thumbnails:
                if SHUTDOWN_REQUESTED:
                    break

                bbl = thumb['bbl']
                url = thumb['url']

                if not bbl:
                    stats['no_bbl'] += 1
                    continue

                # Skip if already downloaded
                if bbl in downloaded_bbls:
                    stats['skip'] += 1
                    pbar.update(1)
                    continue

                # Get save path
                bin_id = bbl_map.get(bbl)
                save_path = get_save_path(borough, bbl, bin_id)

                if save_path.exists():
                    stats['skip'] += 1
                    downloaded_bbls.add(bbl)
                    pbar.update(1)
                    continue

                if dry_run:
                    stats['success'] += 1
                    pbar.update(1)
                    continue

                # Download and save
                if download_and_save_image(url, save_path, session):
                    stats['success'] += 1
                    downloaded_bbls.add(bbl)
                else:
                    stats['fail'] += 1

                pbar.update(1)
                pbar.set_postfix({
                    'ok': stats['success'],
                    'skip': stats['skip'],
                    'fail': stats['fail']
                })

            # Save progress after each page
            progress['downloaded_bbls'] = list(downloaded_bbls)
            progress['last_page'][str(borough)] = page
            save_progress(progress)

            page += 1

            # Check if we've processed all items
            if page * items_per_page >= total:
                break

            time.sleep(PAGE_DELAY)

        pbar.close()

    # Final save
    progress['downloaded_bbls'] = list(downloaded_bbls)
    save_progress(progress)

    print("\n" + "="*60)
    print("SCRAPER COMPLETE")
    print("="*60)
    print(f"  Downloaded: {stats['success']:,}")
    print(f"  Skipped: {stats['skip']:,}")
    print(f"  Failed: {stats['fail']:,}")
    print(f"  No BBL: {stats['no_bbl']:,}")
    print(f"  Output: {OUTPUT_BASE}")
    print("="*60)


def test_mode(driver):
    """Quick test to verify scraping works."""
    print("\n" + "="*60)
    print("TEST MODE")
    print("="*60)

    col_name, col_id = BOROUGH_COLLECTIONS[1]  # Manhattan
    url = f"{PRESERVICA_ACCESS}/uncategorized/{col_id}/"

    print(f"\n[Test] Navigating to {col_name}...")
    driver.get(url)
    time.sleep(3)

    print(f"[Test] Page title: {driver.title}")
    print(f"[Test] Current URL: {driver.current_url[:80]}...")

    total = get_total_results(driver)
    print(f"[Test] Total results: {total:,}")

    thumbnails = extract_thumbnails_from_page(driver)
    print(f"[Test] Thumbnails found on page: {len(thumbnails)}")

    if thumbnails:
        print("\n[Test] Sample thumbnails:")
        for t in thumbnails[:5]:
            print(f"  - BBL: {t['bbl']}, Title: {t['title'][:40]}...")
            print(f"    URL: {t['url'][:80]}...")

    print("\n" + "="*60)
    print("TEST COMPLETE")
    print("="*60)


def main():
    parser = argparse.ArgumentParser(description="Tax Photo Scraper V3 (Selenium)")
    parser.add_argument("--show-browser", action="store_true", help="Show browser window")
    parser.add_argument("--dry-run", action="store_true", help="Don't download, just simulate")
    parser.add_argument("--test", action="store_true", help="Test mode - verify scraping works")
    parser.add_argument("--boroughs", default="1,2,3,4,5", help="Boroughs to process")
    args = parser.parse_args()

    print("\n[Browser] Starting...")
    driver = create_driver(headless=not args.show_browser)

    try:
        # Initial navigation
        driver.get(PRESERVICA_ACCESS)
        time.sleep(2)

        print("\n" + "="*60)
        print("SETUP")
        print("="*60)
        print("1. Browser should show NYC Municipal Archives")
        print("2. If there's a Cloudflare challenge, solve it")
        print("3. Press ENTER when you can see the site...")
        print("="*60)
        input("\nPress ENTER to continue...")

        if args.test:
            test_mode(driver)
        else:
            # Load BBL->BIN mapping
            bbl_map = get_supabase_mapping()

            boroughs = [int(b) for b in args.boroughs.split(",")]
            run_scraper(driver, boroughs, bbl_map, args.dry_run)

    finally:
        driver.quit()
        print("[Browser] Closed")


if __name__ == "__main__":
    main()
