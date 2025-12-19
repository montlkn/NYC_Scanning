#!/usr/bin/env python3
"""
High-Performance LPC Async Scraper with Session Hijacking.

Strategy:
1. "Turbo Mode": Uses browser ONCE to solve Cloudflare.
2. Steals cookies for high-speed httpx async requests.
3. Prioritizes named landmarks for maximum impact.
4. Includes safety throttles (rate limits, random delays) to avoid IP bans.

Usage:
    python scripts/scrape_lpc_async.py --limit 1000
    python scripts/scrape_lpc_async.py --limit 500 --dry-run
"""

import os
import sys
import re
import argparse
import asyncio
import time
import random
import psycopg2
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from io import BytesIO
from PIL import Image

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Load environment variables
from dotenv import load_dotenv
load_dotenv(dotenv_path=backend_dir / '.env')

from models.config import get_settings
from utils.storage import s3_client

# Selenium for initial auth
import undetected_chromedriver as uc

LPC_BASE_URL = "https://nyclandmarks.lunaimaging.com"

# --- Safety Configuration ---
MAX_CONCURRENT_REQUESTS = 3  # Limit parallel downloads
MIN_DELAY = 1.0              # Min seconds between requests
MAX_DELAY = 2.5              # Max seconds between requests

async def get_verified_session_data() -> Dict:
    """Launch browser, wait for user verify, return cookies + user-agent."""
    print("🚀 Launching browser for initial verification...")
    
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1200,800")
    
    driver = uc.Chrome(options=options, use_subprocess=True)
    
    try:
        driver.get(LPC_BASE_URL)
        
        print("\n" + "!"*60)
        print("PLEASE VERIFY IN BROWSER NOW")
        print("1. Solve Cloudflare challenge.")
        print("2. Wait for the LPC home page.")
        print("3. Press ENTER here when done.")
        print("!"*60 + "\n")
        # Use simple input, assuming run in terminal
        await asyncio.to_thread(input, "Press ENTER to continue...")
        
        # Grab credentials
        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        user_agent = driver.execute_script("return navigator.userAgent;")
        
        print("✅ Credentials captured! Switching to Turbo Mode.")
        return {
            "cookies": cookies,
            "user_agent": user_agent
        }
        
    finally:
        driver.quit()
        print("🛑 Browser closed.")

async def search_lpc_async(client: httpx.AsyncClient, query: str) -> Optional[str]:
    """
    Search LPC using httpx. Returns the FIRST valid media_id found.
    Uses regex on raw HTML since we don't have a DOM.
    """
    url = f"{LPC_BASE_URL}/luna/servlet/view/search"
    params = {"q": query}
    
    try:
        # Add random delay before request to be safe
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        
        resp = await client.get(url, params=params)
        
        # DEBUG: Save first response to file to inspect HTML
        if not os.path.exists("debug_lpc_response.html"):
            with open("debug_lpc_response.html", "w") as f:
                f.write(resp.text)
            print(f"DEBUG: Saved HTML to debug_lpc_response.html (Status: {resp.status_code})")
        
        if resp.status_code != 200:
            return None
            
        # Parse for Media IDs in HTML
        # Look for the pattern we found: NYClandmarks~2~2~<ID1>~<ID2>
        # Pattern in HTML often appears in id="" or data attributes
        # We look for the composite ID pattern
        
        # This regex looks for the pattern we saw in the 'id' attribute
        matches = re.findall(r'NYClandmarks~\d+~\d+~(\d+~\d+)', resp.text)
        
        if matches:
            return matches[0] # Return first match
            
        return None
            
    except Exception as e:
        print(f"❌ Error searching {query}: {e}")
        return None

def get_high_res_url(media_id: str) -> str:
    return f"{LPC_BASE_URL}/luna/servlet/iiif/NYClandmarks~2~2~{media_id}/full/800,/0/default.jpg"

async def process_building(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    building: Dict,
    dry_run: bool
) -> bool:
    """
    Process a single building with concurrency limits.
    """
    async with sem:  # Acquire semaphore
        bin_id = str(building['bin']).replace('.0', '')
        name = building.get('building_name') or ''
        address = building.get('address') or ''
        
        # Prioritize address search
        search_terms = []
        if address: search_terms.append(address)
        if name: search_terms.append(name)
        
        for term in search_terms:
            media_id = await search_lpc_async(client, term)
            
            if media_id:
                image_url = get_high_res_url(media_id)
                print(f"✅ {bin_id} | Found: {media_id} | {term[:20]}...")
                
                if dry_run:
                    return True
                
                # Download & Upload
                try:
                    # random delay for download too
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    
                    img_resp = await client.get(image_url)
                    if img_resp.status_code == 200:
                        # Upload logic (sync S3 client wrapped in thread)
                        await asyncio.to_thread(
                            upload_to_r2_sync, 
                            img_resp.content,
                            bin_id
                        )
                        return True
                except Exception as e:
                    print(f"❌ Upload failed {bin_id}: {e}")
                    
        print(f"⚠️ {bin_id} | No images found")
        return False

def upload_to_r2_sync(image_bytes: bytes, bin_id: str):
    """Sync wrapper for S3 upload."""
    settings = get_settings()
    try:
        img = Image.open(BytesIO(image_bytes))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        output = BytesIO()
        img.save(output, format='JPEG', quality=85, optimize=True)
        output.seek(0)
        
        key = f"buildings/{bin_id}/lpc_facade.jpg"
        s3_client.put_object(
            Bucket=settings.r2_bucket,
            Key=key,
            Body=output.read(),
            ContentType='image/jpeg',
            ACL='public-read'
        )
    except Exception as e:
        print(f"Upload error: {e}")
        raise e

async def main():
    parser = argparse.ArgumentParser(description="LPC Async Scraper")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    # 1. Get Session
    session_data = await get_verified_session_data()
    
    # 2. Get Buildings from DB
    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()
    
    print("📊 Fetching building list...")
    # Prioritize buildings with names (likely landmarks)
    cur.execute("""
        SELECT bin, building_name, address
        FROM buildings_full_merge_scanning
        WHERE building_name IS NOT NULL 
          AND building_name != ''
        ORDER BY building_name
        OFFSET %s
        LIMIT %s
    """, (args.offset, args.limit))
    
    rows = cur.fetchall()
    conn.close()
    
    print(f"⚡ Starting Async Scrape for {len(rows)} buildings...")
    if args.dry_run: print("🔍 DRY RUN MODE")
    
    # 3. Setup Async Client
    headers = {
        "User-Agent": session_data['user_agent'],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": LPC_BASE_URL,
    }
    
    async with httpx.AsyncClient(headers=headers, cookies=session_data['cookies'], timeout=30.0, follow_redirects=True) as client:
        # Validate session
        await client.get(LPC_BASE_URL)
        
        # Semaphore for concurrency
        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        
        tasks = []
        for row in rows:
            building = {'bin': row[0], 'building_name': row[1], 'address': row[2]}
            tasks.append(process_building(sem, client, building, args.dry_run))
        
        # Run all tasks
        results = await asyncio.gather(*tasks)
        
        success_count = sum(1 for r in results if r)
        print("\n" + "="*60)
        print(f"✅ COMPLETE: {success_count}/{len(rows)} found ({success_count/len(rows)*100:.1f}%)")

if __name__ == "__main__":
    asyncio.run(main())
