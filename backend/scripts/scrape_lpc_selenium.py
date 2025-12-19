#!/usr/bin/env python3
"""
Parallel LPC Scraper
--------------------
Scrapes NYC Landmarks Preservation Commission (LPC) photos using multiple parallel browsers.
Significantly faster than the sequential version.

Architecture:
1. Master Process: Connects to DB, handles login, manages task queue.
2. Worker Processes: Consume building tasks, run Selenium searches in parallel.
3. Shared Session: Workers inherit cookies from Master to bypass Cloudflare.

Usage:
    python scripts/scrape_lpc_selenium.py --limit 1000 --workers 6 --show-browser
"""

import os
import sys
import re
import argparse
import time
import psycopg2
import random
import multiprocessing
import traceback
import json
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

# Selenium
import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import requests

# Constants
LPC_BASE_URL = "https://nyclandmarks.lunaimaging.com"
LOCAL_STORAGE_BASE_DIR = Path("/Users/lucienmount/Library/Mobile Documents/com~apple~CloudDocs/sourced_images")

# Global shutdown flag for workers
SHUTDOWN_REQUESTED = False

def signal_handler(signum, frame):
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True

# ============================================================================
# UTILS
# ============================================================================

def create_driver(headless: bool = True):
    """Create optimized Chrome driver."""
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    
    options.page_load_strategy = 'eager'
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1200,800")
    options.add_argument("--mute-audio")
    
    return uc.Chrome(options=options, use_subprocess=True)

def get_high_res_url(media_id: str) -> str:
    return f"{LPC_BASE_URL}/luna/servlet/iiif/NYClandmarks~2~2~{media_id}/full/800,/0/default.jpg"

def download_image(url: str, session: requests.Session) -> Optional[bytes]:
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 1000:
            return resp.content
    except: pass
    return None

def save_to_local(image_bytes: bytes, bin_id: str, media_id: str) -> str:
    target_dir = LOCAL_STORAGE_BASE_DIR / str(bin_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    
    img = Image.open(BytesIO(image_bytes))
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
    
    filename = f"{media_id}.jpg"
    file_path = target_dir / filename
    
    img.save(file_path, format='JPEG', quality=95)
    return str(file_path)

# ============================================================================
# SEARCH LOGIC (Worker)
# ============================================================================

def search_lpc(driver: webdriver.Chrome, query: str) -> List[Dict]:
    """Search for a query string and return results."""
    results = []
    try:
        search_url = f"{LPC_BASE_URL}/luna/servlet/view/search?q={query}"
        driver.get(search_url)

        # Smart Wait
        try:
            WebDriverWait(driver, 4).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, "img.thumbImg") or 
                          "No matching results" in d.page_source
            )
        except: pass 

        thumbnails = driver.find_elements(By.CSS_SELECTOR, "img.thumbImg")
        if not thumbnails: return []

        found_ids = set()
        for thumb in thumbnails:
            try:
                img_id_attr = thumb.get_attribute("id")
                media_id = None
                if img_id_attr:
                    match = re.search(r'NYClandmarks~\d+~\d+~(\d+~\d+)', img_id_attr)
                    if match: media_id = match.group(1)
                
                if media_id and media_id not in found_ids:
                    results.append({"media_id": media_id})
                    found_ids.add(media_id)
                    if len(results) >= 5: break # Limit per search term
            except: continue
            
        return results
    except: return []

def process_building_task(driver, session, building, dry_run=False):
    """
    Worker task: Process one building.
    Returns: (status, count, bin_id)
    """
    bin_id = str(building['bin']).replace('.0', '')
    name = building.get('building_name') or ''
    address = building.get('address') or ''

    # Check if already done
    local_folder = LOCAL_STORAGE_BASE_DIR / str(bin_id)
    if local_folder.exists() and any(local_folder.iterdir()):
        return 'SKIP', 0, bin_id

    # Search Terms
    search_terms = []
    if address: search_terms.append(address)
    if name and name != '0': search_terms.append(name)
    if bin_id: search_terms.append(bin_id)

    downloaded = set()
    
    for term in search_terms:
        results = search_lpc(driver, term)
        for item in results:
            media_id = item['media_id']
            if media_id in downloaded: continue
            
            if dry_run:
                downloaded.add(media_id)
                continue

            url = get_high_res_url(media_id)
            data = download_image(url, session)
            if data:
                try:
                    save_to_local(data, bin_id, media_id)
                    downloaded.add(media_id)
                except: pass
        
        if len(downloaded) >= 5: break
    
    if len(downloaded) > 0:
        return 'SUCCESS', len(downloaded), bin_id
    return 'NO_RESULTS', 0, bin_id


# ============================================================================
# WORKER PROCESS
# ============================================================================

def worker_process(worker_id, task_queue, result_queue, cookies, dry_run):
    driver = None
    try:
        # Launch Browser
        driver = create_driver(headless=True)
        
        # Auth
        driver.get(LPC_BASE_URL)
        time.sleep(1)
        for c in cookies:
            try: driver.add_cookie(c)
            except: pass
        driver.refresh()
        
        # Request Session (for image downloads)
        session = requests.Session()
        session.headers['User-Agent'] = driver.execute_script("return navigator.userAgent")

        while True:
            try:
                task = task_queue.get(timeout=2)
            except:
                time.sleep(1)
                continue
            
            if task == 'STOP': break
            
            building = task
            status, count, bin_id = process_building_task(driver, session, building, dry_run)
            result_queue.put((status, count, bin_id))
            
    except Exception as e:
        print(f"[Worker {worker_id}] Error: {e}")
        traceback.print_exc()
    finally:
        if driver: driver.quit()

# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Parallel LPC Scraper")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-browser", action="store_true") # Only affects Master
    args = parser.parse_args()

    print("\n" + "="*60)
    print("PARALLEL LPC SCRAPER")
    print("="*60)
    print(f"  Workers: {args.workers}")
    print(f"  Target: {LOCAL_STORAGE_BASE_DIR}")
    
    # 1. Master Browser & Auth
    print("\n[Master] Opening browser for login...")
    master_driver = create_driver(headless=not args.show_browser)
    try:
        master_driver.get(LPC_BASE_URL)
        print("\n" + "!"*60)
        print("LOGIN REQUIRED")
        print("1. Solve Cloudflare challenge in the browser window.")
        print("2. Ensure you see the LPC search page.")
        print("3. Press ENTER here when ready.")
        print("!"*60 + "\n")
        input("Press ENTER to start...")
        
        cookies = master_driver.get_cookies()
        master_driver.quit() # We don't need master browser anymore
        print(f"[Master] Captured {len(cookies)} cookies.")
        
    except Exception as e:
        print(f"Error: {e}")
        return

    # 2. Get Buildings
    print("\n[DB] Fetching building list...")
    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT bin, building_name, address
        FROM buildings_full_merge_scanning
        WHERE building_name IS NOT NULL AND building_name != ''
        ORDER BY building_name
        OFFSET %s LIMIT %s
    """, (args.offset, args.limit))
    rows = cur.fetchall()
    conn.close()
    
    buildings = [{'bin': r[0], 'building_name': r[1], 'address': r[2]} for r in rows]
    print(f"[DB] Found {len(buildings)} buildings to process.")

    # 3. Start Workers
    task_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    
    print(f"[System] Spawning {args.workers} workers...")
    workers = []
    for i in range(args.workers):
        p = multiprocessing.Process(
            target=worker_process,
            args=(i, task_queue, result_queue, cookies, args.dry_run)
        )
        p.start()
        workers.append(p)

    # 4. Fill Queue
    for b in buildings:
        task_queue.put(b)
    
    # 5. Monitor
    stats = {'success': 0, 'skip': 0, 'no_results': 0, 'images': 0}
    
    with tqdm(total=len(buildings), desc="Processing") as pbar:
        completed = 0
        while completed < len(buildings):
            try:
                status, count, bin_id = result_queue.get(timeout=1)
                completed += 1
                
                if status == 'SUCCESS':
                    stats['success'] += 1
                    stats['images'] += count
                elif status == 'SKIP':
                    stats['skip'] += 1
                else:
                    stats['no_results'] += 1
                
                pbar.update(1)
                pbar.set_postfix(ok=stats['success'], imgs=stats['images'])
            except:
                # Timeout, continue waiting
                pass

    # 6. Cleanup
    print("\n[System] Stopping workers...")
    for _ in workers: task_queue.put('STOP')
    for w in workers: w.join()
    
    print("\n" + "="*60)
    print("COMPLETE")
    print(f"  Buildings with images: {stats['success']}")
    print(f"  Total images found: {stats['images']}")
    print(f"  Skipped (already done): {stats['skip']}")
    print("="*60)

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
