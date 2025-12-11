"""
Cloudflare R2 storage utilities for image uploads
"""

import asyncio
import boto3
from botocore.client import Config
from io import BytesIO
from typing import Optional
import logging
from PIL import Image

from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Initialize S3 client for R2
s3_client = boto3.client(
    's3',
    endpoint_url=f'https://{settings.r2_account_id}.r2.cloudflarestorage.com',
    aws_access_key_id=settings.r2_access_key_id,
    aws_secret_access_key=settings.r2_secret_access_key,
    config=Config(signature_version='s3v4'),
    region_name='auto'
)


async def upload_image(
    image_bytes: bytes,
    key: str,
    content_type: str = 'image/jpeg',
    make_public: bool = True,
    create_thumbnail: bool = False
) -> str:
    """
    Upload image to Cloudflare R2 (uses default building-images bucket)

    Args:
        image_bytes: Image bytes to upload
        key: Object key/path in bucket
        content_type: MIME type
        make_public: Whether to make publicly accessible
        create_thumbnail: Whether to also create and upload thumbnail

    Returns:
        Public URL of uploaded image
    """
    return await upload_image_to_bucket(
        image_bytes=image_bytes,
        key=key,
        bucket=settings.r2_bucket,
        public_url=settings.r2_public_url,
        content_type=content_type,
        make_public=make_public,
        create_thumbnail=create_thumbnail
    )


async def upload_image_to_bucket(
    image_bytes: bytes,
    key: str,
    bucket: str,
    public_url: str,
    content_type: str = 'image/jpeg',
    make_public: bool = True,
    create_thumbnail: bool = False
) -> str:
    """
    Upload image to a specific Cloudflare R2 bucket

    Args:
        image_bytes: Image bytes to upload
        key: Object key/path in bucket
        bucket: Bucket name (e.g., 'building-images' or 'user-images')
        public_url: Public URL base for this bucket
        content_type: MIME type
        make_public: Whether to make publicly accessible
        create_thumbnail: Whether to also create and upload thumbnail

    Returns:
        Public URL of uploaded image
    """
    try:
        # Upload main image in thread pool (boto3 is synchronous)
        await asyncio.to_thread(
            s3_client.put_object,
            Bucket=bucket,
            Key=key,
            Body=image_bytes,
            ContentType=content_type,
            ACL='public-read' if make_public else 'private'
        )

        image_url = f"{public_url}/{key}"
        logger.info(f"Uploaded image to R2 ({bucket}): {key}")

        # Optionally create thumbnail
        thumbnail_url = None
        if create_thumbnail:
            thumbnail_key = key.replace('.jpg', '_thumb.jpg')
            thumbnail_bytes = create_thumbnail_bytes(image_bytes)
            if thumbnail_bytes:
                await asyncio.to_thread(
                    s3_client.put_object,
                    Bucket=bucket,
                    Key=thumbnail_key,
                    Body=thumbnail_bytes,
                    ContentType=content_type,
                    ACL='public-read' if make_public else 'private'
                )
                thumbnail_url = f"{public_url}/{thumbnail_key}"
                logger.info(f"Uploaded thumbnail to R2 ({bucket}): {thumbnail_key}")

        return image_url

    except Exception as e:
        logger.error(f"Failed to upload image to R2 ({bucket}): {e}", exc_info=True)
        raise


def create_thumbnail_bytes(image_bytes: bytes, size: tuple = (200, 200)) -> Optional[bytes]:
    """
    Create thumbnail from image bytes

    Args:
        image_bytes: Original image bytes
        size: Thumbnail size (width, height)

    Returns:
        Thumbnail bytes or None if failed
    """
    try:
        img = Image.open(BytesIO(image_bytes))
        img.thumbnail(size, Image.Resampling.LANCZOS)

        # Save to bytes
        output = BytesIO()
        img.save(output, format='JPEG', quality=85, optimize=True)
        output.seek(0)

        return output.read()

    except Exception as e:
        logger.error(f"Failed to create thumbnail: {e}")
        return None


async def upload_from_url(
    source_url: str,
    destination_key: str,
    create_thumbnail: bool = False
) -> str:
    """
    Download image from URL and upload to R2

    Args:
        source_url: URL to download from
        destination_key: Key to store in R2
        create_thumbnail: Whether to create thumbnail

    Returns:
        Public URL of uploaded image
    """
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(source_url)
        response.raise_for_status()
        image_bytes = response.content

    return await upload_image(
        image_bytes,
        destination_key,
        create_thumbnail=create_thumbnail
    )


async def delete_image(key: str) -> bool:
    """
    Delete image from R2

    Args:
        key: Object key to delete

    Returns:
        True if successful, False otherwise
    """
    try:
        await asyncio.to_thread(
            s3_client.delete_object,
            Bucket=settings.r2_bucket,
            Key=key
        )
        logger.info(f"Deleted image from R2: {key}")
        return True

    except Exception as e:
        logger.error(f"Failed to delete image from R2: {e}")
        return False


async def get_image_url(key: str) -> str:
    """
    Get public URL for an image

    Args:
        key: Object key

    Returns:
        Public URL
    """
    return f"{settings.r2_public_url}/{key}"
