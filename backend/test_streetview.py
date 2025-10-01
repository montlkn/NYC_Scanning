"""
Test script for Google Street View API integration
Tests fetching and processing street view images
"""

import asyncio
from PIL import Image
from io import BytesIO
import logging

from services.reference_images import fetch_street_view
from models.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


async def test_streetview_api_key():
    """Test that API key is configured"""
    logger.info("Testing Google Maps API key configuration...")

    if not settings.google_maps_api_key:
        logger.error("❌ GOOGLE_MAPS_API_KEY not set in .env")
        return False

    logger.info(f"✅ API key configured: {settings.google_maps_api_key[:10]}...")
    return True


async def test_fetch_streetview_image():
    """Test fetching a street view image"""
    logger.info("\nTesting Street View image fetch...")

    # Empire State Building
    lat = 40.7484
    lng = -73.9857
    heading = 45  # Northeast

    try:
        image_bytes = await fetch_street_view(lat, lng, heading)

        if not image_bytes:
            logger.error("❌ No image data returned")
            return False

        logger.info(f"✅ Received {len(image_bytes)} bytes")

        # Verify it's a valid image
        image = Image.open(BytesIO(image_bytes))
        logger.info(f"✅ Valid image: {image.format} {image.size[0]}x{image.size[1]} pixels")
        logger.info(f"✅ Image mode: {image.mode}")

        return True

    except Exception as e:
        logger.error(f"❌ Failed to fetch image: {e}", exc_info=True)
        return False


async def test_streetview_invalid_location():
    """Test handling of invalid location (middle of ocean)"""
    logger.info("\nTesting invalid location handling...")

    # Middle of Atlantic Ocean
    lat = 0.0
    lng = -30.0
    heading = 0

    try:
        image_bytes = await fetch_street_view(lat, lng, heading)

        if image_bytes:
            logger.warning("⚠️  Got image for ocean location (unexpected, but Google sometimes has coverage)")
            return True
        else:
            logger.info("✅ Correctly returned None for invalid location")
            return True

    except Exception as e:
        logger.error(f"❌ Exception on invalid location: {e}")
        return False


async def test_streetview_multiple_headings():
    """Test fetching multiple views of same location"""
    logger.info("\nTesting multiple headings...")

    # Flatiron Building
    lat = 40.7411
    lng = -73.9897

    headings = [0, 90, 180, 270]  # N, E, S, W

    for heading in headings:
        try:
            image_bytes = await fetch_street_view(lat, lng, heading)

            if image_bytes:
                image = Image.open(BytesIO(image_bytes))
                logger.info(f"✅ Heading {heading}°: {len(image_bytes)} bytes, {image.size}")
            else:
                logger.warning(f"⚠️  Heading {heading}°: No image returned")

        except Exception as e:
            logger.error(f"❌ Heading {heading}° failed: {e}")
            return False

    return True


async def main():
    """Run all Street View API tests"""
    logger.info("=" * 60)
    logger.info("GOOGLE STREET VIEW API TESTS")
    logger.info("=" * 60)

    results = []

    # Test 1: API key
    results.append(await test_streetview_api_key())

    if not results[0]:
        logger.error("\n❌ API key not configured - skipping other tests")
        return

    # Test 2: Basic fetch
    results.append(await test_fetch_streetview_image())

    # Test 3: Invalid location
    results.append(await test_streetview_invalid_location())

    # Test 4: Multiple headings
    results.append(await test_streetview_multiple_headings())

    logger.info("\n" + "=" * 60)
    if all(results):
        logger.info("✅ ALL STREET VIEW TESTS PASSED")
    else:
        logger.error(f"❌ {results.count(False)} test(s) failed")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
