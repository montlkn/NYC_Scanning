#!/usr/bin/env python3
"""
Tax Photo Scraper V4 (Parallel) - OpenSeadragon/DZI Render API

Discovery: The site uses OpenSeadragon Deep Zoom Image format!
Strategy:
1. Main process opens "Master" browser for login & pagination.
2. Main process extracts Entity IDs from search pages.
3. Multiple "Worker" processes (each with a browser) pick items from queue.
4. Workers navigate to item page -> get token -> download image.
5. Workers use session cookies from Master to avoid manual login.

Usage:
    python3 scripts/scrape_tax_photos_v4.py --show-browser --workers 4
    python3 scripts/scrape_tax_photos_v4.py --show-browser --boroughs 1 --workers 6
"""

import os
import sys
import re
import json
import argparse
import time
import signal
import requests
import math
import multiprocessing
import traceback
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple, Any
from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from PIL import Image
from tqdm import tqdm
import xml.etree.ElementTree as ET

# Global shutdown flag (for main process)
SHUTDOWN_REQUESTED = False

def signal_handler(signum, frame):
    global SHUTDOWN_REQUESTED
    print("\n\n>>> Shutdown requested! Stopping workers...")
    SHUTDOWN_REQUESTED = True

# Only set signal handler in main process/thread to avoid conflicts
if __name__ == "__main__":
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

# ============================================================================
# CONFIGURATION
# ============================================================================

PRESERVICA_ACCESS = "https://nycrecords.access.preservica.com"
PRESERVICA_RENDER = "https://nycrecords.preservica.com"

BOROUGH_COLLECTIONS = {
    1: ("Manhattan", "SO_975f712b-36ad-47b7-9cbe-cc3903b25a28"),
    2: ("Bronx", "SO_61d09827-787f-453c-a003-b1ccc2017c9e"),
    3: ("Brooklyn", "SO_fdf29daa-2b53-4fd9-b1b7-1280b7a02b8a"),
    4: ("Queens", "SO_0fc6d8aa-5ed8-4da7-986a-8804668b3c10"),
    5: ("Staten Island", "SO_d1a15702-bc15-474b-9663-c1820d5ae2e3"),
}

BOROUGH_NAMES = {
    1: "Manhattan", 2: "Bronx", 3: "Brooklyn", 4: "Queens", 5: "Staten_Island"
}

OUTPUT_BASE = Path("/Users/lucienmount/Library/Mobile Documents/com~apple~CloudDocs/tax_photos_1980s")
PROGRESS_FILE = Path(__file__).parent / "tax_photos_v4_progress.json"

# ============================================================================
# UTILS
# ============================================================================

def parse_bbl_from_identifier(identifier: str, borough: int = 1) -> Optional[str]:
    if not identifier: return None
    # dof_1_00001_0010
    m = re.search(r'dof_(\d)_(\d{5})_(\d{4})', identifier, re.IGNORECASE)
    if m: return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    # lvd_04_00013 -> Borough + Block + Lot
    m = re.search(r'lvd_(\d{2})_(\d{5})', identifier, re.IGNORECASE)
    if m:
        block = int(m.group(1))
        lot = int(m.group(2))
        return f"{borough}{block:05d}{lot:04d}"
    return None

def get_save_path(borough: int, bbl: str, bin_id: Optional[str] = None) -> Path:
    borough_name = BOROUGH_NAMES.get(borough, str(borough))
    folder = bin_id if bin_id else f"unmatched_{bbl}"
    return OUTPUT_BASE / borough_name / folder / f"tax_lot_image_1980_{bbl}.jpg"

def load_progress() -> Dict:
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {'downloaded': [], 'last_page': {}, 'skipped_outtakes': []}

def save_progress(progress: Dict):
    progress['last_updated'] = datetime.now().isoformat()
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)

def get_supabase_mapping() -> Dict[str, str]:
    from models.config import get_settings
    try:
        settings = get_settings()
        conn = psycopg2.connect(settings.database_url)
        cur = conn.cursor()
        print("\n[DB] Loading BBL->BIN mapping...")
        cur.execute("SELECT bbl, bin FROM buildings_full_merge_scanning WHERE bbl IS NOT NULL AND bin IS NOT NULL")
        rows = cur.fetchall()
        conn.close()
        mapping = {}
        for bbl, bin_id in rows:
            clean_bbl = str(bbl).replace(".0", "").replace("-", "").replace(" ", "")
            if len(clean_bbl) == 10:
                mapping[clean_bbl] = str(bin_id).replace(".0", "")
        print(f"[DB] Loaded {len(mapping):,} mappings")
        return mapping
    except Exception as e:
        print(f"[DB] Warning: Could not load mapping: {e}")
        return {}

def should_skip_item(title: str) -> bool:
    if not title: return True
    t = title.lower().strip()
    return 'outtake' in t or not t

def get_file_hash(path: Path) -> str:
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except:
        return ""

# ============================================================================
# BROWSER
# ============================================================================

def create_driver(headless: bool = False):
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.page_load_strategy = 'eager'
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1200,800")
    # Mute audio/popups
    options.add_argument("--mute-audio")
    options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    return uc.Chrome(options=options, use_subprocess=True)

def extract_render_token(driver, expected_entity_id: str = None) -> Optional[str]:
    try:
        logs = driver.get_log('performance')
        for entry in logs:
            try:
                msg = json.loads(entry['message'])
                url = msg.get('message', {}).get('params', {}).get('request', {}).get('url', '')
                if 'Render/render' in url and 'token=' in url:
                    # Validate ID if provided
                    if expected_entity_id and expected_entity_id not in url:
                        continue
                    
                    parsed = urlparse(url)
                    params = parse_qs(parsed.query)
                    if 'token' in params:
                        return params['token'][0]
            except: pass
    except: pass
    return None

def download_image(session: requests.Session, entity_id: str, token: str, save_path: Path) -> bool:
    img_url = f"{PRESERVICA_RENDER}/Render/render/resource/{entity_id}/openseadragon/image?token={token}"
    try:
        resp = session.get(img_url, timeout=30)
        if resp.status_code == 200 and len(resp.content) > 1000:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.open(BytesIO(resp.content))
            if img.mode != 'RGB': img = img.convert('RGB')
            img.save(save_path, format='JPEG', quality=85)
            return True
    except: pass
    return False

# ============================================================================
# WORKER PROCESS
# ============================================================================

def worker_process(
    worker_id: int,
    task_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    cookies: List[Dict],
    bbl_map: Dict[str, str],
    headless: bool
):
    """
    Worker Loop:
    1. Initialize Browser
    2. Set Cookies (Authentication)
    3. Loop: Fetch Item -> Process -> Report Result
    """
    driver = None
    try:
        # print(f"[Worker {worker_id}] Starting browser...")
        driver = create_driver(headless=headless)
        
        # Authenticate
        driver.get(PRESERVICA_ACCESS)
        time.sleep(2)
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
            except: pass
        driver.refresh()
        time.sleep(2)
        
        # Setup Request Session
        session = requests.Session()
        session.headers['User-Agent'] = driver.execute_script("return navigator.userAgent")
        for c in driver.get_cookies():
            session.cookies.set(c['name'], c['value'])

        # print(f"[Worker {worker_id}] Ready.")
        recent_hashes = []

        while True:
            try:
                task = task_queue.get(timeout=2)
            except:
                # If queue empty, wait a bit and check again, or if main sent 'STOP'
                time.sleep(1)
                continue

            if task == 'STOP':
                break

            entity_id, title, borough, dry_run = task
            
            # 1. Get Identifier from Page
            item_url = f"{PRESERVICA_ACCESS}/uncategorized/IO_{entity_id}/"
            try:
                # Clear stale logs
                driver.get_log('performance')

                driver.get(item_url)
                time.sleep(3.0) # Wait for JS and Network

                if entity_id not in driver.current_url:
                    print(f"[Worker {worker_id}] WARNING: Stuck? Wanted {entity_id}, got {driver.current_url}")

                identifier = None
                # Try title first
                if not identifier:
                    match = re.search(r'(lvd_(\d{2})_(\d{5})|dof_(\d)_(\d{5})_(\d{4}))', driver.title, re.IGNORECASE)
                    if match:
                        if match.group(2) and match.group(3): # lvd format
                            identifier = f"lvd_{match.group(2)}_{match.group(3)}"
                        elif match.group(4) and match.group(5) and match.group(6): # dof format
                            identifier = f"dof_{match.group(4)}_{match.group(5)}_{match.group(6)}"
                
                # Try body text
                if not identifier:
                    text = driver.find_element(By.TAG_NAME, "body").text
                    match = re.search(r'(lvd_(\d{2})_(\d{5})|dof_(\d)_(\d{5})_(\d{4}))', text, re.IGNORECASE)
                    if match:
                        if match.group(2) and match.group(3): # lvd format
                            identifier = f"lvd_{match.group(2)}_{match.group(3)}"
                        elif match.group(4) and match.group(5) and match.group(6): # dof format
                            identifier = f"dof_{match.group(4)}_{match.group(5)}_{match.group(6)}"

                if not identifier:
                    result_queue.put(('SKIP_NO_ID', entity_id))
                    continue

                # 2. Parse BBL
                bbl = parse_bbl_from_identifier(identifier, borough)
                if not bbl:
                    result_queue.put(('SKIP_NO_BBL', entity_id))
                    continue

                # 3. Check Existence
                bin_id = bbl_map.get(bbl)
                save_path = get_save_path(borough, bbl, bin_id)
                
                if save_path.exists():
                    result_queue.put(('SKIP_EXISTS', entity_id))
                    continue
                
                if dry_run:
                    result_queue.put(('SUCCESS_DRY', entity_id))
                    continue

                # 4. Get Token & Download
                token = extract_render_token(driver, expected_entity_id=entity_id)
                if not token:
                    # print(f"[Worker {worker_id}] No token found for {entity_id}")
                    result_queue.put(('FAIL_TOKEN', entity_id))
                    continue

                if download_image(session, entity_id, token, save_path):
                    # Check for duplicates (Stuck browser detection)
                    file_hash = get_file_hash(save_path)
                    if file_hash in recent_hashes:
                        print(f"[Worker {worker_id}] WARNING: Duplicate image content detected! (Stuck browser?)")
                        # Optional: Remove the bad file
                        # try: save_path.unlink()
                        # except: pass
                    
                    recent_hashes.append(file_hash)
                    if len(recent_hashes) > 5: recent_hashes.pop(0)

                    result_queue.put(('SUCCESS', entity_id))
                else:
                    result_queue.put(('FAIL_DOWNLOAD', entity_id))
            
            except Exception as e:
                # print(f"[Worker {worker_id}] Error: {e}")
                result_queue.put(('ERROR', entity_id))
                # Reset driver on error
                try:
                    driver.get(PRESERVICA_ACCESS)
                except:
                    pass

    except Exception as e:
        print(f"[Worker {worker_id}] CRASHED: {e}")
        traceback.print_exc()
    finally:
        if driver:
            driver.quit()
        # print(f"[Worker {worker_id}] Stopped.")


# ============================================================================
# MAIN PRODUCER
# ============================================================================

def extract_entity_ids_from_page(driver) -> List[Tuple[str, str]]:
    """Get (entity_id, title) from search page."""
    results = []
    try:
        # Find links with IO_ prefix
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/IO_']")
        seen = set()
        for link in links:
            try:
                href = link.get_attribute('href')
                m = re.search(r'IO_([a-f0-9-]+)', href)
                if m and m.group(1) not in seen:
                    eid = m.group(1)
                    title = link.get_attribute('title') or link.text.strip()
                    seen.add(eid)
                    results.append((eid, title))
            except: pass
    except: pass
    return results

def main():
    parser = argparse.ArgumentParser(description="Tax Photo Scraper V4 (Parallel)")
    parser.add_argument("--workers", type=int, default=4, help="Number of browser workers")
    parser.add_argument("--show-browser", action="store_true", help="Show worker browsers")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--boroughs", default="1,2,3,4,5")
    args = parser.parse_args()

    # 1. Setup
    print("\n" + "="*60)
    print("PARALLEL SCRAPER V4")
    print("="*60)
    print(f"  Workers: {args.workers}")
    print(f"  Boroughs: {args.boroughs}")
    
    bbl_map = get_supabase_mapping()
    progress = load_progress()
    downloaded = set(progress.get('downloaded', []))
    print(f"  Already downloaded: {len(downloaded):,}")

    # 2. Authenticate Master
    print("\n[Master] Launching browser for authentication...")
    master_driver = create_driver(headless=False) # Always show master for login
    try:
        # Go to collection
        col_id = BOROUGH_COLLECTIONS[1][1]
        master_driver.get(f"{PRESERVICA_ACCESS}/uncategorized/{col_id}/")
        
        print("\n" + "="*60)
        print("LOGIN REQUIRED")
        print("="*60)
        print("1. Please solve Cloudflare / Log in on the Chrome window.")
        print("2. Ensure you can see the grid of images.")
        print("3. Press ENTER here when ready.")
        print("="*60)
        input("Press ENTER to start workers...")

        cookies = master_driver.get_cookies()
        print(f"[Master] Captured {len(cookies)} cookies.")
        
    except Exception as e:
        print(f"[Master] Error: {e}")
        master_driver.quit()
        return

    # 3. Start Workers
    print(f"\n[System] Spawning {args.workers} workers...")
    task_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    
    workers = []
    for i in range(args.workers):
        p = multiprocessing.Process(
            target=worker_process,
            args=(i, task_queue, result_queue, cookies, bbl_map, not args.show_browser)
        )
        p.start()
        workers.append(p)

    # 4. Processing Loop
    stats = {
        'success': 0, 'skip': 0, 'skip_outtake': 0, 
        'fail': 0, 'no_id': 0, 'no_bbl': 0
    }
    
    boroughs = [int(b) for b in args.boroughs.split(",")]
    
    try:
        for borough in boroughs:
            col_name, col_id = BOROUGH_COLLECTIONS[borough]
            print(f"\n[Borough] {col_name}...")
            
            # Start at page 1 or last saved page
            page = progress.get('last_page', {}).get(str(borough), 1)
            base_url = f"{PRESERVICA_ACCESS}/uncategorized/{col_id}/"

            while True:
                # Navigate Master
                url = f"{base_url}?pg={page}" if page > 1 else base_url
                master_driver.get(url)
                time.sleep(3) # Wait for render

                # Extract Items
                items = extract_entity_ids_from_page(master_driver)
                
                if not items:
                    print(f"  [Master] Page {page}: No items found (End of collection?)")
                    break
                
                # Filter & Queue Items
                batch_count = 0
                for eid, title in items:
                    if eid in downloaded:
                        stats['skip'] += 1
                        continue
                    if should_skip_item(title):
                        stats['skip_outtake'] += 1
                        downloaded.add(eid)
                        continue
                    
                    task_queue.put((eid, title, borough, args.dry_run))
                    batch_count += 1
                
                if batch_count == 0:
                    print(f"  [Master] Page {page}: All items skipped.")
                    page += 1
                    continue

                # Wait for batch to finish
                print(f"  [Master] Page {page}: Processing {batch_count} items with {args.workers} workers...")
                
                completed = 0
                with tqdm(total=batch_count, leave=False) as pbar:
                    while completed < batch_count:
                        if SHUTDOWN_REQUESTED: break
                        
                        try:
                            res_type, res_id = result_queue.get(timeout=1)
                            completed += 1
                            
                            downloaded.add(res_id)
                            
                            if res_type.startswith('SUCCESS'): stats['success'] += 1
                            elif res_type == 'SKIP_EXISTS': stats['skip'] += 1
                            elif res_type == 'SKIP_NO_ID': stats['no_id'] += 1
                            elif res_type == 'SKIP_NO_BBL': stats['no_bbl'] += 1
                            else: stats['fail'] += 1
                            
                            pbar.update(1)
                            pbar.set_postfix(ok=stats['success'], fail=stats['fail'])
                        except:
                            pass # Timeout

                if SHUTDOWN_REQUESTED: break

                # Save Progress
                progress['downloaded'] = list(downloaded)
                progress['last_page'][str(borough)] = page
                save_progress(progress)
                
                page += 1

            if SHUTDOWN_REQUESTED: break

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        # Cleanup
        print("\n[System] Stopping workers...")
        for _ in workers: task_queue.put('STOP')
        for w in workers: w.join(timeout=5)
        master_driver.quit()
        
        # Final Stats
        print("\n" + "="*60)
        print("SCRAPE COMPLETE")
        print("="*60)
        print(f"  Success: {stats['success']:,}")
        print(f"  Skipped: {stats['skip']:,}")
        print(f"  Outtakes: {stats['skip_outtake']:,}")
        print(f"  Failures: {stats['fail']:,}")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()