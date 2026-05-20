"""
Debug API endpoints - For development and testing only
"""

from fastapi import APIRouter, Query, HTTPException
from typing import Optional
import logging

from services import geospatial, clip_matcher
from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()



@router.get("/test-clip")
async def test_clip():
    """
    Test CLIP model loading and basic inference
    """
    try:
        model, preprocess, device = clip_matcher.get_clip_model()

        return {
            'status': 'ok',
            'model': settings.clip_model_name,
            'device': str(device),
            'model_loaded': model is not None,
            'preprocess_loaded': preprocess is not None
        }

    except Exception as e:
        logger.error(f"Test CLIP failed: {e}")
        return {
            'status': 'error',
            'error': str(e),
            'message': 'CLIP model not loaded. Did you call init_clip_model()?'
        }


@router.get("/test-bearing")
async def test_bearing_calculation(
    lat1: float = Query(..., description="Start latitude"),
    lng1: float = Query(..., description="Start longitude"),
    lat2: float = Query(..., description="End latitude"),
    lng2: float = Query(..., description="End longitude")
):
    """
    Test bearing calculation between two points
    """
    bearing = geospatial.calculate_bearing(lat1, lng1, lat2, lng2)

    return {
        'from': {'lat': lat1, 'lng': lng1},
        'to': {'lat': lat2, 'lng': lng2},
        'bearing': round(bearing, 2),
        'bearing_compass': get_compass_direction(bearing)
    }


def get_compass_direction(bearing: float) -> str:
    """Convert bearing to compass direction"""
    directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    index = round(bearing / (360 / len(directions))) % len(directions)
    return directions[index]


@router.get("/config")
async def get_configuration():
    """
    Get current configuration (non-sensitive values)
    """
    return {
        'env': settings.env,
        'debug': settings.debug,
        'clip_model': settings.clip_model_name,
        'clip_device': settings.clip_device,
        'max_scan_distance': settings.max_scan_distance_meters,
        'cone_angle': settings.cone_angle_degrees,
        'max_candidates': settings.max_candidates,
        'confidence_threshold': settings.confidence_threshold,
        'landmark_boost': settings.landmark_boost_factor,
        'proximity_boost_threshold': settings.proximity_boost_threshold,
        'proximity_boost_factor': settings.proximity_boost_factor,
    }


@router.get("/health-detailed")
async def detailed_health_check():
    """
    Detailed health check for debugging
    """
    checks = {
        'api': 'ok',
        'clip_model': 'unknown',
        'database': 'unknown',
        'redis': 'unknown',
        'storage': 'unknown'
    }

    # Test CLIP model
    try:
        clip_matcher.get_clip_model()
        checks['clip_model'] = 'ok'
    except Exception as e:
        checks['clip_model'] = f'error: {str(e)}'

    # TODO: Test database connection
    # TODO: Test Redis connection
    # TODO: Test R2 storage

    all_ok = all(v == 'ok' for v in checks.values())

    return {
        'status': 'healthy' if all_ok else 'degraded',
        'checks': checks
    }


@router.post("/test-image-comparison")
async def test_image_comparison(
    image_url_1: str = Query(..., description="First image URL"),
    image_url_2: str = Query(..., description="Second image URL")
):
    """
    Test CLIP image comparison between two URLs
    """
    try:
        # Download and encode both images
        img1 = await clip_matcher.download_image(image_url_1)
        img2 = await clip_matcher.download_image(image_url_2)

        if not img1 or not img2:
            raise HTTPException(status_code=400, detail="Failed to download images")

        emb1 = clip_matcher.encode_image(img1)
        emb2 = clip_matcher.encode_image(img2)

        if emb1 is None or emb2 is None:
            raise HTTPException(status_code=500, detail="Failed to encode images")

        # Calculate similarity
        import torch
        similarity = (emb1 @ emb2.T).item()
        confidence = (similarity + 1) / 2  # Normalize to 0-1

        return {
            'image_1': image_url_1,
            'image_2': image_url_2,
            'similarity': round(similarity, 4),
            'confidence': round(confidence, 4),
            'match_quality': (
                'excellent' if confidence > 0.8 else
                'good' if confidence > 0.7 else
                'moderate' if confidence > 0.6 else
                'poor'
            )
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Test image comparison failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# test-street-view endpoint removed 2026-05-15 along with the
# services/reference_images module — the production Street View fetcher
# lives in services/clip_disambiguation.fetch_street_view_image. For
# manual diagnostics use scripts/inspect_refs.py instead.