"""
Test script for Cloudflare R2 storage integration
Tests image upload, retrieval, and deletion
"""

import asyncio
from PIL import Image
from io import BytesIO
import logging

from utils.storage import upload_image, delete_image, get_image_url
from models.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


def create_test_image(size=(600, 600), color=(255, 0, 0)):
    """Create a test image in memory"""
    img = Image.new('RGB', size, color)
    img_bytes = BytesIO()
    img.save(img_bytes, format='JPEG', quality=90)
    img_bytes.seek(0)
    return img_bytes.getvalue()


async def test_r2_credentials():
    """Test that R2 credentials are configured"""
    logger.info("Testing Cloudflare R2 configuration...")

    required = [
        ('R2_ACCOUNT_ID', settings.r2_account_id),
        ('R2_ACCESS_KEY_ID', settings.r2_access_key_id),
        ('R2_SECRET_ACCESS_KEY', settings.r2_secret_access_key),
        ('R2_BUCKET', settings.r2_bucket),
        ('R2_PUBLIC_URL', settings.r2_public_url)
    ]

    all_configured = True
    for name, value in required:
        if not value:
            logger.error(f"❌ {name} not set in .env")
            all_configured = False
        else:
            logger.info(f"✅ {name} configured: {str(value)[:20]}...")

    return all_configured


async def test_upload_image():
    """Test uploading an image to R2"""
    logger.info("\nTesting image upload to R2...")

    try:
        # Create a test image
        image_bytes = create_test_image(color=(255, 0, 0))  # Red image
        logger.info(f"Created test image: {len(image_bytes)} bytes")

        # Upload to R2
        path = "test/test_image_001.jpg"
        result_url = await upload_image(image_bytes, path)

        if result_url:
            logger.info(f"✅ Image uploaded successfully")
            logger.info(f"✅ URL: {result_url}")
            return path
        else:
            logger.error("❌ Upload returned None")
            return None

    except Exception as e:
        logger.error(f"❌ Upload failed: {e}", exc_info=True)
        return None


async def test_get_image_url(path):
    """Test getting public URL for uploaded image"""
    logger.info("\nTesting public URL generation...")

    try:
        url = await get_image_url(path)
        logger.info(f"✅ Public URL: {url}")

        # Check URL format
        if settings.r2_public_url in url and path in url:
            logger.info("✅ URL format correct")
            return True
        else:
            logger.error("❌ URL format incorrect")
            return False

    except Exception as e:
        logger.error(f"❌ Failed to get URL: {e}")
        return False


async def test_upload_multiple_images():
    """Test uploading multiple images"""
    logger.info("\nTesting multiple image uploads...")

    paths = []
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]  # Red, Green, Blue

    for i, color in enumerate(colors):
        try:
            image_bytes = create_test_image(color=color)
            path = f"test/multi_test_{i:03d}.jpg"

            result = await upload_image(image_bytes, path)

            if result:
                logger.info(f"✅ Uploaded image {i+1}/3: {path}")
                paths.append(path)
            else:
                logger.error(f"❌ Failed to upload image {i+1}")
                return paths

        except Exception as e:
            logger.error(f"❌ Upload {i+1} failed: {e}")
            return paths

    logger.info(f"✅ Successfully uploaded {len(paths)} images")
    return paths


async def test_delete_image(path):
    """Test deleting an image from R2"""
    logger.info(f"\nTesting image deletion: {path}...")

    try:
        result = await delete_image(path)

        if result:
            logger.info(f"✅ Image deleted successfully")
            return True
        else:
            logger.warning("⚠️  Delete returned False (may not exist)")
            return False

    except Exception as e:
        logger.error(f"❌ Delete failed: {e}", exc_info=True)
        return False


async def cleanup_test_images(paths):
    """Clean up all test images"""
    logger.info("\nCleaning up test images...")

    for path in paths:
        try:
            await delete_image(path)
            logger.info(f"✅ Cleaned up: {path}")
        except Exception as e:
            logger.warning(f"⚠️  Failed to clean up {path}: {e}")


async def main():
    """Run all R2 storage tests"""
    logger.info("=" * 60)
    logger.info("CLOUDFLARE R2 STORAGE TESTS")
    logger.info("=" * 60)

    results = []
    uploaded_paths = []

    # Test 1: Credentials
    results.append(await test_r2_credentials())

    if not results[0]:
        logger.error("\n❌ R2 credentials not configured - skipping other tests")
        return

    # Test 2: Upload single image
    path = await test_upload_image()
    if path:
        uploaded_paths.append(path)
        results.append(True)

        # Test 3: Get URL
        results.append(await test_get_image_url(path))
    else:
        results.append(False)
        results.append(False)

    # Test 4: Upload multiple images
    multi_paths = await test_upload_multiple_images()
    if len(multi_paths) == 3:
        results.append(True)
        uploaded_paths.extend(multi_paths)
    else:
        results.append(False)

    # Test 5: Delete image
    if uploaded_paths:
        results.append(await test_delete_image(uploaded_paths[0]))

    # Cleanup
    await cleanup_test_images(uploaded_paths)

    logger.info("\n" + "=" * 60)
    if all(results):
        logger.info("✅ ALL R2 STORAGE TESTS PASSED")
    else:
        logger.error(f"❌ {results.count(False)} test(s) failed")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
