"""
Reference image fetch — Google Street View only.

Historical context (2026-05-15): this used to be a Mapillary→Google chain, but
Mapillary's crowd-sourced panos were CLIP-poisoned with car-interior shots
(see docs/scanning_strategy_2026-05-15.md). With CLIP zeroed in the pipeline,
the chain is now a thin pass-through to the paid Google fetcher.

NYC tax photos were considered but excluded: 1940s/1980s archival imagery
teaches CLIP a building's historical facade — actively misleading.
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


async def fetch_reference_image(
    *,
    lat: float,
    lng: float,
    bbl: Optional[str],  # accepted but currently unused; kept for forward compat
    google_fallback,     # callable: (lat, lng) -> Optional[bytes]
) -> Tuple[Optional[bytes], str]:
    """
    Fetch a reference image via Google Street View. Returns
    (image_bytes, source_label) where source_label is 'google_streetview' or 'none'.
    """
    img = await google_fallback(lat, lng)
    if img:
        return img, "google_streetview"
    return None, "none"
