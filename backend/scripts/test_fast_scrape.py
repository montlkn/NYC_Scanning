#!/usr/bin/env python3
"""
FAST Scraper Proof-of-Concept for LPC Luna Archive.
Uses "Session Hijacking" to bypass Cloudflare + Selenium bottleneck.

Workflow:
1. Launches Browser ONCE for you to solve Cloudflare.
2. Steals the valid cookies (cf_clearance, etc.).
3. Uses lightweight HTTP requests for high-speed scraping.
"""

import sys
import argparse
import asyncio
import httpx
import time
import re
from pathlib import Path
from typing import List, Dict

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Selenium imports
import undetected_chromedriver as uc

LPC_BASE_URL = "https://nyclandmarks.lunaimaging.com"

# --- 1. Browser Setup (Same as before) ---
def get_verified_session() -> Dict:
    """Launch browser, wait for user verify, return cookies + user-agent."""
    print("🚀 Launching browser for verification...")
    
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
        input("Press ENTER to continue...")
        
        # Grab credentials
        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        user_agent = driver.execute_script("return navigator.userAgent;")
        
        print("✅ Credentials captured!")
        return {
            "cookies": cookies,
            "user_agent": user_agent
        }
        
    finally:
        driver.quit()
        print("🛑 Browser closed.")


# --- 2. High-Speed Scraper ---
async def fetch_building(client: httpx.AsyncClient, address: str) -> str:
    """Fetch search results for a building using the stolen session."""
    url = f"{LPC_BASE_URL}/luna/servlet/view/search"
    params = {"q": address}
    
    try:
        start = time.time()
        resp = await client.get(url, params=params)
        duration = time.time() - start
        
        # Check if we got blocked or success
        if resp.status_code == 200 and "Luna" in resp.text:
            # Quick check for images
            count = len(re.findall(r'NYClandmarks~\d+~\d+~\d+~\d+', resp.text))
            return f"✅ {address[:20]}... | {duration:.2f}s | Found ~{count} IDs"
        elif resp.status_code in [403, 503]:
            return f"❌ {address[:20]}... | {duration:.2f}s | BLOCKED ({resp.status_code})"
        else:
            return f"⚠️ {address[:20]}... | {duration:.2f}s | Status {resp.status_code}"
            
    except Exception as e:
        return f"❌ Error: {e}"


async def run_fast_scrape(session_data: Dict):
    """Run concurrent requests using the session."""
    headers = {
        "User-Agent": session_data['user_agent'],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": LPC_BASE_URL,
        "Origin": LPC_BASE_URL,
    }
    
    test_addresses = [
        "350 Fifth Avenue",
        "153 Franklin Street",
        "430 Lafayette Street",
        "434 Lafayette Street",
        "432 Lafayette Street",
        "225-227 Front Street",
        "100 Broadway",
        "1000 Fifth Avenue",
        "10 Grand Army Plaza",
        "89 East 42nd Street"
    ]
    
    print(f"\n⚡ Starting HIGH-SPEED test on {len(test_addresses)} buildings...")
    
    async with httpx.AsyncClient(headers=headers, cookies=session_data['cookies'], follow_redirects=True) as client:
        # First request to warm up connection/check validity
        print("   Validating session with first request...")
        await client.get(LPC_BASE_URL)
        
        tasks = [fetch_building(client, addr) for addr in test_addresses]
        results = await asyncio.gather(*tasks)
        
        for res in results:
            print(res)

if __name__ == "__main__":
    # 1. Get credentials
    session_data = get_verified_session()
    
    # 2. Run fast test
    asyncio.run(run_fast_scrape(session_data))
