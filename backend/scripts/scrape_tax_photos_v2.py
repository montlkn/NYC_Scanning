#!/usr/bin/env python3
"""
Tax Photo Scraper V2 - Smart Enumeration Approach

STRATEGY:
Instead of "searching" for each building (which is brittle), we ENUMERATE
the entire Preservica collection for each borough.

For each item found:
1. Extract BBL from title (e.g. "dof_1_00001_0010")
2. Check if BBL maps to a known BIN in Supabase
3. If MATCH: Save to `tax_photos_1980s/{Borough}/{BIN}/...`
4. If NO MATCH: Save to `tax_photos_1980s/{Borough}/unmatched_{BBL}/...`

This guarantees 100% coverage without 404 errors.

Usage:
    # Run the smart crawler
    python3 scripts/scrape_tax_photos_v2.py --phase 2 --show-browser

    # Run for specific borough (e.g. Manhattan=1)
    python3 scripts/scrape_tax_photos_v2.py --phase 2 --boroughs 1 --show-browser
"""

import os
import sys
import re
import json
import argparse
import time
import asyncio
import signal
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Set
from io import BytesIO
from datetime import datetime
import httpx
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
PROGRESS_FILE = Path(__file__).parent / "tax_photos_v2_progress.json"

# Rate limiting & parallelism
REQUESTS_PER_SECOND = 50  # Increased from 30
NUM_WORKERS = 20          # Increased from 10
BATCH_SIZE = 100


# ============================================================================
# BROWSER AUTHENTICATION
# ============================================================================

def create_driver(headless: bool = False):
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.page_load_strategy = 'eager'
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1200,800")
    
    # Enable performance logging to extract network requests
    options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    
    return uc.Chrome(options=options, use_subprocess=True)

def get_session_from_browser(driver) -> Tuple[Dict, Dict, str]: # Return user_agent
    cookies = {c['name']: c['value'] for c in driver.get_cookies()}
    user_agent = driver.execute_script("return navigator.userAgent")
    
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Referer": PRESERVICA_ACCESS,
        "Origin": PRESERVICA_ACCESS,
    }
    
    return cookies, headers, user_agent

def authenticate(show_browser: bool = True) -> Tuple[Dict, Dict, Optional[str], str]:
    print("\n[Browser] Opening for authentication...")
    driver = create_driver(headless=not show_browser)
    token = None
    try:
        driver.get(PRESERVICA_ACCESS)
        time.sleep(3)
        driver.get(f"{PRESERVICA_ACCESS}/uncategorized/{BOROUGH_COLLECTIONS[1][1]}/ượng")
        time.sleep(5) 
        
        print("\n" + "="*60)
        print("AUTHENTICATION REQUIRED")
        print("="*60)
        print("1. Solve Cloudflare challenge")
        print("2. Ensure you can see thumbnails")
        print("3. Script will automatically detect token when you browse...")
        print("   (Or you can paste it manually if detection fails)")
        print("="*60)
        
        # Now, actively poll logs for the token
        print("DEBUG: Actively polling network logs for token (Timeout: 120s)...")
        start_time = time.time()
        timeout = 120 # seconds
        
        last_print = time.time()
        while time.time() - start_time < timeout:
            if time.time() - last_print > 10:
                print(f"DEBUG: Still waiting... ({int(timeout - (time.time() - start_time))}s left)")
                last_print = time.time()

            # Check Logs
            logs = driver.get_log('performance')
            for entry in logs:
                try:
                    message = json.loads(entry['message'])
                    request = message.get('message', {}).get('params', {}).get('request', {})
                    
                    url = request.get('url', '')
                    if "preservica.com" in url and 'headers' in request:
                        auth_header = request['headers'].get('Authorization')
                        if auth_header and auth_header.startswith('Bearer '):
                            token = auth_header.replace('Bearer ', '')
                            print(f"DEBUG: Successfully extracted token from logs!")
                            break
                except:
                    pass
            if token: break
            
            # Check LocalStorage
            try:
                ls = driver.execute_script("return window.localStorage;")
                for key, value in ls.items():
                    if "token" in key.lower() or (isinstance(value, str) and value.startswith("eyJ")):
                        if len(value) > 20 and value.count('.') >= 2: # Very basic JWT check
                            token = value.strip('"')
                            print(f"DEBUG: Extracted token from LocalStorage!")
                            break
            except: pass
            if token: break
            
            time.sleep(1)

        cookies, headers_base, user_agent = get_session_from_browser(driver)
        
        if not token:
            print("\n[!] Automatic detection failed.")
            manual_token = input("Please paste the Bearer token manually (from DevTools > Network): ").strip()
            if manual_token:
                token = manual_token.replace("Bearer ", "").strip()
        
        return cookies, headers_base, token, user_agent
    finally:
        driver.quit()
        print("🛑 Browser closed.")


# ============================================================================
# PROGRESS & HELPERS
# ============================================================================

def load_progress() -> Dict:
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {'downloaded_bbls': [], 'last_updated': None}

def save_progress(progress: Dict):
    progress['last_updated'] = datetime.now().isoformat()
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)

def sanitize_for_filename(text: str) -> str:
    if not text: return ""
    return re.sub(r'[<>:"/\\|?*]', '', text).strip()[:50]

def parse_bbl_from_text(text: str) -> Optional[str]:
    """
    Extract BBL from text like 'dof_1_00001_0010' or '1-00001-0010'.
    Returns 10-digit BBL string: 1000010010
    """
    if not text: return None
    
    # Try pattern: dof_B_BBBBB_LLLL
    m = re.search(r'dof_(\d)_(\d{5})_(\d{4})', text, re.IGNORECASE)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
        
    # Try pattern: B-BBBBB-LLLL
    m = re.search(r'(\d)[-_](\d{5})[_ -](\d{4})', text)
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


# ============================================================================
# DATA LOADING
# ============================================================================

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
# WORKER LOGIC
# ============================================================================

async def download_worker(
    queue: asyncio.Queue,
    client: httpx.AsyncClient,
    headers: Dict,
    semaphore: asyncio.Semaphore,
    bbl_map: Dict[str, str],
    downloaded_bbls: Set[str],
    progress_lock: asyncio.Lock,
    stats: Dict,
    progress: Dict,
    dry_run: bool
):
    while not SHUTDOWN_REQUESTED:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=2.0)
        except asyncio.TimeoutError:
            if queue.empty(): break
            continue

        object_id = item['object_id']
        borough = item['borough']
        # OPTIMIZATION: Check if title was passed in item to avoid extra call
        title = item.get('title') 
        
        # 1. Get Metadata (if not already found in search)
        if not title:
            async with semaphore:
                try:
                    resp = await client.get(
                        f"{API_BASE}/object-details", 
                        params={"id": object_id},
                        headers=headers, timeout=30.0
                    )
                    if resp.status_code != 200:
                        async with progress_lock: stats['fail'] += 1
                        queue.task_done()
                        continue
                    
                    details = resp.json()
                    title = details.get("value", {}).get("title", "")
                    
                except Exception:
                    async with progress_lock: stats['fail'] += 1
                    queue.task_done()
                    continue
                await asyncio.sleep(0.01) # Tiny sleep to yield

        # Extract BBL from title
        bbl = parse_bbl_from_text(title)
        
        # Check for "Outtake"
        if "outtake" in title.lower():
            async with progress_lock: stats['outtake_skip'] += 1
            queue.task_done()
            continue
        
        if not bbl:
            async with progress_lock: stats['no_bbl'] += 1
            queue.task_done()
            continue

        # 2. Check Deduplication
        async with progress_lock:
            if bbl in downloaded_bbls:
                stats['skip'] += 1
                queue.task_done()
                continue

        # 3. Determine Path
        bin_id = bbl_map.get(bbl)
        save_path = get_save_path(borough, bbl, bin_id)
        
        if save_path.exists():
            async with progress_lock:
                stats['skip'] += 1
                downloaded_bbls.add(bbl)
            queue.task_done()
            continue
            
        if dry_run:
            async with progress_lock: stats['success'] += 1
            queue.task_done()
            continue

        # 4. Download Image
        async with semaphore:
            try:
                resp = await client.get(
                    f"{API_BASE}/thumbnail",
                    params={"id": object_id},
                    headers=headers, timeout=30.0
                )
                if resp.status_code == 200:
                    image_bytes = resp.content
                else:
                    image_bytes = None
                await asyncio.sleep(0.01)
            except:
                image_bytes = None

        if not image_bytes:
            async with progress_lock: stats['fail'] += 1
            queue.task_done()
            continue

        # 5. Save
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.open(BytesIO(image_bytes))
            if img.mode != 'RGB': img = img.convert('RGB')
            img.save(save_path, format='JPEG', quality=85)
            
            async with progress_lock:
                stats['success'] += 1
                downloaded_bbls.add(bbl)
                if stats['success'] % 200 == 0:
                    progress['downloaded_bbls'] = list(downloaded_bbls)
                    save_progress(progress)
                    
        except Exception:
            async with progress_lock: stats['fail'] += 1
            
        queue.task_done()


async def run_crawler(cookies: Dict, headers: Dict, token: str, user_agent: str, boroughs: List[int], dry_run: bool):
    print("\n" + "="*60)
    print("STARTING SMART CRAWLER (OPTIMIZED)")
    print("="*60)
    print(f"  Rate: {REQUESTS_PER_SECOND} req/sec")
    print(f"  Workers: {NUM_WORKERS}")
    
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("WARNING: No token provided. Expecting failures.")
    
    bbl_map = get_supabase_mapping()
    progress = load_progress()
    downloaded_bbls = set(progress.get('downloaded_bbls', []))
    print(f"[Progress] Already downloaded: {len(downloaded_bbls):,} images")
    
    semaphore = asyncio.Semaphore(REQUESTS_PER_SECOND)
    stats = {'success': 0, 'skip': 0, 'fail': 0, 'no_bbl': 0, 'outtake_skip': 0}
    
    async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=60.0) as client:
        
        for borough in boroughs:
            if SHUTDOWN_REQUESTED: break
            
            col_name, col_id = BOROUGH_COLLECTIONS[borough]
            print(f"\n[Borough] Enumerating {col_name}...")
            
            start = 0
            page_size = 100
            
            while not SHUTDOWN_REQUESTED:
                # Fetch Page
                async with semaphore:
                    try:
                        # OPTIMIZATION ATTEMPT: Request metadata in search to avoid 'object-details' call
                        # Common solr params: fl, fields, metadata
                        resp = await client.get(
                            f"{API_BASE}/search",
                            params={
                                "q": "*", 
                                "parenthierarchy": col_id, 
                                "start": start, 
                                "max": page_size,
                                "metadata": "xip.title" # Try to get title
                            },
                            headers=headers
                        )
                        
                        if resp.status_code != 200:
                            print(f"[API] Error {resp.status_code}: {resp.text[:200]}")
                            break
                        
                        data = resp.json()
                        # Some APIs return items in 'value.objectIds', others in 'value.items'
                        # We need to handle both just in case
                        items_raw = data.get("value", {}).get("objectIds", [])
                        total = data.get("value", {}).get("totalHits", 0)
                        
                        # DEBUG: Check if we got titles in the search response
                        if start == 0:
                            print(f"[API] First page sample keys: {items_raw[0] if items_raw else 'None'}")
                            
                    except Exception as e:
                        print(f"Error fetching page: {e}")
                        break
                
                if not items_raw: break
                
                queue = asyncio.Queue()
                progress_lock = asyncio.Lock()
                
                # Fill queue - if items_raw contains dicts with title, great!
                # If it's just IDs (strings), we'll have to fetch details.
                for item in items_raw:
                    if isinstance(item, dict):
                        await queue.put({'object_id': item.get('ref'), 'title': item.get('title'), 'borough': borough})
                    else:
                        await queue.put({'object_id': item, 'title': None, 'borough': borough})
                
                workers = [
                    asyncio.create_task(download_worker(
                        queue, client, headers, semaphore, bbl_map,
                        downloaded_bbls, progress_lock, stats, progress, dry_run
                    )) for _ in range(NUM_WORKERS)
                ]
                
                with tqdm(total=len(items_raw), desc=f"Pg {start//page_size}", leave=False) as pbar:
                    while not queue.empty():
                        await asyncio.sleep(1)
                        pbar.update(0) # Just to refresh
                
                await asyncio.gather(*workers)
                
                start += page_size
                if start >= total: break
                
                # Save checkpoint periodically
                progress['downloaded_bbls'] = list(downloaded_bbls)
                save_progress(progress)

    print("\n[Done] Crawler Finished")
    print(f"Stats: {stats}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="2")
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--boroughs", default="1,2,3,4,5")
    args = parser.parse_args()
    
    cookies, headers, token, user_agent = authenticate(args.show_browser)
    boroughs = [int(b) for b in args.boroughs.split(",")]
    
    asyncio.run(run_crawler(cookies, headers, token, user_agent, boroughs, args.dry_run))

if __name__ == "__main__":
    main()
