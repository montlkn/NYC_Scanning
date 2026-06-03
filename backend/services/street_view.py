"""
Google Street View Static API helper.

Extracted from the deleted services/clip_disambiguation.py so the scan pipeline
can fetch reference imagery without pulling in any CLIP/torch code.
"""

import logging
from typing import Optional

import httpx

from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def fetch_street_view_image(
    lat: float,
    lng: float,
    heading: float,
    pitch: int = 10,
    fov: int = 60,
    size: str = "400x400",
) -> Optional[bytes]:
    url = (
        f"https://maps.googleapis.com/maps/api/streetview?"
        f"size={size}&"
        f"location={lat},{lng}&"
        f"heading={heading}&"
        f"pitch={pitch}&"
        f"fov={fov}&"
        f"key={settings.google_maps_api_key}"
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            if response.status_code != 200:
                logger.error(f"Street View API error: {response.status_code}")
                return None
            if len(response.content) <= 5000:
                logger.warning("Street View returned placeholder (no imagery)")
                return None
            return response.content
    except Exception as e:
        logger.error(f"Failed to fetch Street View: {e}")
        return None
