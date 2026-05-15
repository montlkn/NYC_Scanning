"""
Inspect the Google Street View reference image that CLIP would see for each BIN.

Uses the facade-aware centerline camera pose (camera_pose_for_bin) — same pose
the production pipeline now uses — and fetches a 640x640 Street View frame
(prod uses 400x400; we go bigger here purely for human inspection).

Usage:
    python scripts/inspect_refs.py 1042010 1042007 1042006

Output:
    /tmp/jink_refs/<bin>_<heading>deg.jpg per BIN, opened in Preview on macOS.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / "backend" / ".env")

FOOTPRINTS_DB_URL = os.environ["FOOTPRINTS_DB_URL"]
GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]

OUT_DIR = Path("/tmp/jink_refs")


async def get_pose(bin_val: str) -> tuple[float, float, float, str | None] | None:
    """Return (cam_lat, cam_lng, heading_deg, street_name) or None if no pose."""
    conn = await asyncpg.connect(FOOTPRINTS_DB_URL)
    try:
        row = await conn.fetchrow(
            "SELECT cam_lat, cam_lng, heading_deg, street_name "
            "FROM camera_pose_for_bin($1)",
            bin_val,
        )
    finally:
        await conn.close()
    if row is None or row["cam_lat"] is None:
        return None
    return (
        float(row["cam_lat"]),
        float(row["cam_lng"]),
        float(row["heading_deg"]),
        row["street_name"],
    )


async def fetch_street_view(
    lat: float, lng: float, heading: float, size: str = "640x640",
    pitch: int = 25, fov: int = 90,
) -> bytes | None:
    url = (
        "https://maps.googleapis.com/maps/api/streetview"
        f"?size={size}&location={lat},{lng}&heading={heading}"
        f"&pitch={pitch}&fov={fov}&key={GOOGLE_MAPS_API_KEY}"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        print(f"  ! HTTP {resp.status_code}")
        return None
    if len(resp.content) < 5000:
        # Google returns a tiny "no imagery here" placeholder when there's no
        # coverage; threshold matches production fetch_street_view_image.
        print(f"  ! placeholder ({len(resp.content)}B) — no Street View coverage")
        return None
    return resp.content


async def inspect_bin(bin_val: str) -> Path | None:
    print(f"\n=== BIN {bin_val} ===")
    pose = await get_pose(bin_val)
    if pose is None:
        print("  ! camera_pose_for_bin returned no row")
        return None
    lat, lng, heading, street = pose
    print(f"  pose: ({lat:.6f}, {lng:.6f}) heading={heading:.0f}°  street={street!r}")

    img = await fetch_street_view(lat, lng, heading)
    if img is None:
        return None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{bin_val}_{int(heading)}deg.jpg"
    out.write_bytes(img)
    print(f"  ✓ saved {out} ({len(img)//1024}KB)")
    return out


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bins", nargs="+", help="BINs to inspect")
    parser.add_argument("--no-open", action="store_true", help="Don't open in Preview")
    args = parser.parse_args()

    files: list[Path] = []
    for bin_val in args.bins:
        out = await inspect_bin(bin_val)
        if out is not None:
            files.append(out)

    if files and not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", *map(str, files)], check=False)


if __name__ == "__main__":
    asyncio.run(main())
