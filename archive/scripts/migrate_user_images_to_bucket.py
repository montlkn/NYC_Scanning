"""
Migration script to move user images from building-images/user-images/
to the new user-images bucket.

This script:
1. Lists all objects in building-images/user-images/
2. Copies them to the new user-images bucket
3. Optionally deletes from the old location (with confirmation)

Usage:
    python backend/scripts/migrate_user_images_to_bucket.py [--delete-old]
"""

import boto3
from botocore.client import Config
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Get the project root directory (nyc_scan/)
project_root = Path(__file__).parent.parent.parent
backend_dir = Path(__file__).parent.parent

# Load .env file from backend directory
env_path = backend_dir / '.env'
if env_path.exists():
    load_dotenv(env_path)
    print(f"Loaded .env from: {env_path}")
else:
    print(f"Warning: .env not found at {env_path}")

# Add backend to path
sys.path.insert(0, str(backend_dir))

from models.config import get_settings

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


def list_user_images():
    """List all objects in building-images/user-images/"""
    print(f"Listing objects in {settings.r2_bucket}/user-images/...")

    user_images = []
    paginator = s3_client.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=settings.r2_bucket, Prefix='user-images/'):
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                # Skip the folder itself
                if key != 'user-images/' and not key.endswith('/'):
                    user_images.append({
                        'key': key,
                        'size': obj['Size'],
                        'last_modified': obj['LastModified']
                    })

    return user_images


def migrate_image(old_key: str, dry_run: bool = False):
    """
    Copy image from building-images to user-images bucket

    Transform:
        building-images/user-images/{user_id}/{BIN}/{filename}
        -> user-images/{user_id}/{BIN}/{filename}
    """
    # Remove 'user-images/' prefix
    new_key = old_key.replace('user-images/', '', 1)

    if dry_run:
        print(f"  [DRY RUN] Would copy: {old_key} -> {new_key}")
        return True

    try:
        # Copy to new bucket
        s3_client.copy_object(
            Bucket=settings.r2_user_images_bucket,
            CopySource={'Bucket': settings.r2_bucket, 'Key': old_key},
            Key=new_key,
            ACL='public-read'
        )
        print(f"  ✓ Copied: {old_key} -> {new_key}")
        return True

    except Exception as e:
        print(f"  ✗ Failed to copy {old_key}: {e}")
        return False


def delete_old_image(key: str):
    """Delete image from old location"""
    try:
        s3_client.delete_object(Bucket=settings.r2_bucket, Key=key)
        print(f"  ✓ Deleted old: {key}")
        return True
    except Exception as e:
        print(f"  ✗ Failed to delete {key}: {e}")
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Migrate user images to new bucket')
    parser.add_argument('--delete-old', action='store_true',
                        help='Delete images from old location after copying')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    args = parser.parse_args()

    print("=" * 80)
    print("User Images Bucket Migration")
    print("=" * 80)
    print(f"Source: {settings.r2_bucket}/user-images/")
    print(f"Destination: {settings.r2_user_images_bucket}/")
    print()

    # Check if user-images bucket exists
    try:
        s3_client.head_bucket(Bucket=settings.r2_user_images_bucket)
        print(f"✓ Destination bucket '{settings.r2_user_images_bucket}' exists")
    except Exception as e:
        print(f"✗ Error: Destination bucket '{settings.r2_user_images_bucket}' does not exist!")
        print(f"  Please create it in Cloudflare R2 dashboard first.")
        print(f"  Error: {e}")
        return 1

    print()

    # List user images
    user_images = list_user_images()

    if not user_images:
        print("No user images found in building-images/user-images/")
        print("Migration complete (nothing to migrate).")
        return 0

    print(f"Found {len(user_images)} user images to migrate")
    print()

    if args.dry_run:
        print("DRY RUN MODE - No changes will be made")
        print()

    # Confirm migration
    if not args.dry_run:
        response = input(f"Migrate {len(user_images)} images? (y/n): ")
        if response.lower() != 'y':
            print("Migration cancelled")
            return 0
        print()

    # Migrate images
    print("Migrating images...")
    print()

    success_count = 0
    failed_count = 0

    for img in user_images:
        old_key = img['key']

        # Copy to new bucket
        if migrate_image(old_key, dry_run=args.dry_run):
            success_count += 1

            # Delete old if requested
            if args.delete_old and not args.dry_run:
                delete_old_image(old_key)
        else:
            failed_count += 1

    print()
    print("=" * 80)
    print(f"Migration {'preview' if args.dry_run else 'complete'}")
    print(f"  Success: {success_count}")
    print(f"  Failed: {failed_count}")

    if args.dry_run:
        print()
        print("To perform the migration, run without --dry-run:")
        print(f"  python {__file__}")

    print("=" * 80)

    return 0 if failed_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
