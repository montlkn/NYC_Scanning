#!/usr/bin/env python3
"""
Verify Cloudflare R2 bucket structure and list contents
"""

import boto3
from botocore.client import Config
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

def list_buckets():
    """List all R2 buckets"""
    print("=" * 80)
    print("CLOUDFLARE R2 BUCKETS")
    print("=" * 80)

    try:
        response = s3_client.list_buckets()
        buckets = response.get('Buckets', [])

        print(f"\nFound {len(buckets)} bucket(s):\n")
        for bucket in buckets:
            print(f"  📦 {bucket['Name']}")
            print(f"     Created: {bucket['CreationDate']}")

        return [b['Name'] for b in buckets]
    except Exception as e:
        print(f"❌ Error listing buckets: {e}")
        return []


def analyze_bucket_structure(bucket_name, max_objects=100):
    """Analyze the structure of a bucket"""
    print(f"\n{'=' * 80}")
    print(f"BUCKET: {bucket_name}")
    print(f"{'=' * 80}")

    try:
        # List objects with pagination
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket_name, MaxKeys=1000)

        all_keys = []
        total_size = 0

        for page in pages:
            if 'Contents' in page:
                for obj in page['Contents']:
                    all_keys.append(obj['Key'])
                    total_size += obj['Size']

        print(f"\n📊 Total Objects: {len(all_keys)}")
        print(f"💾 Total Size: {total_size / (1024*1024):.2f} MB")

        # Analyze directory structure
        prefixes = {}
        for key in all_keys:
            parts = key.split('/')
            if len(parts) > 1:
                prefix = parts[0]
                prefixes[prefix] = prefixes.get(prefix, 0) + 1

        if prefixes:
            print(f"\n📁 Top-level directories:")
            for prefix, count in sorted(prefixes.items(), key=lambda x: x[1], reverse=True):
                print(f"   {prefix}/  ({count} objects)")

        # Show sample keys
        print(f"\n📄 Sample keys (first {min(20, len(all_keys))}):")
        for key in all_keys[:20]:
            size = next((obj['Size'] for obj in page.get('Contents', []) if obj['Key'] == key), 0)
            print(f"   {key}  ({size / 1024:.1f} KB)")

        if len(all_keys) > 20:
            print(f"   ... and {len(all_keys) - 20} more")

        # Check for expected directories
        print(f"\n🔍 Expected directories check:")
        expected_dirs = ['scans', 'user-images', 'reference', 'building-images']
        for dir_name in expected_dirs:
            exists = any(key.startswith(f"{dir_name}/") for key in all_keys)
            status = "✅" if exists else "❌"
            count = sum(1 for key in all_keys if key.startswith(f"{dir_name}/"))
            print(f"   {status} {dir_name}/  ({count} objects)")

        return all_keys

    except Exception as e:
        print(f"❌ Error analyzing bucket: {e}")
        return []


def check_config():
    """Verify R2 configuration"""
    print(f"\n{'=' * 80}")
    print("CONFIGURATION CHECK")
    print(f"{'=' * 80}\n")

    print(f"R2 Account ID: {settings.r2_account_id[:8]}...")
    print(f"R2 Bucket (from config): {settings.r2_bucket}")
    print(f"R2 Public URL: {settings.r2_public_url}")
    print(f"Access Key ID: {settings.r2_access_key_id[:8]}...")


def test_upload():
    """Test uploading a small file"""
    print(f"\n{'=' * 80}")
    print("UPLOAD TEST")
    print(f"{'=' * 80}\n")

    test_key = "test/connection_test.txt"
    test_content = b"R2 connection test - this file can be deleted"

    try:
        # Upload
        s3_client.put_object(
            Bucket=settings.r2_bucket,
            Key=test_key,
            Body=test_content,
            ContentType='text/plain'
        )
        print(f"✅ Upload successful: {test_key}")

        # Verify
        response = s3_client.get_object(Bucket=settings.r2_bucket, Key=test_key)
        downloaded = response['Body'].read()

        if downloaded == test_content:
            print(f"✅ Verification successful")
        else:
            print(f"❌ Verification failed - content mismatch")

        # Cleanup
        s3_client.delete_object(Bucket=settings.r2_bucket, Key=test_key)
        print(f"✅ Cleanup successful")

        return True

    except Exception as e:
        print(f"❌ Upload test failed: {e}")
        return False


def main():
    print("\n" + "🔍 CLOUDFLARE R2 STRUCTURE VERIFICATION" + "\n")

    # Check configuration
    check_config()

    # List all buckets
    buckets = list_buckets()

    # Analyze configured bucket
    if settings.r2_bucket in buckets:
        analyze_bucket_structure(settings.r2_bucket)
    else:
        print(f"\n❌ WARNING: Configured bucket '{settings.r2_bucket}' not found!")
        print(f"   Available buckets: {', '.join(buckets)}")

        if buckets:
            # Analyze first bucket found
            print(f"\n   Analyzing '{buckets[0]}' instead...")
            analyze_bucket_structure(buckets[0])

    # Test upload
    test_upload()

    print(f"\n{'=' * 80}")
    print("RECOMMENDATIONS")
    print(f"{'=' * 80}\n")

    if 'building-images' not in buckets and 'building_images' in buckets:
        print("⚠️  Bucket name mismatch detected!")
        print("   Your config uses: 'building-images'")
        print("   But bucket exists as: 'building_images'")
        print("\n   Fix options:")
        print("   1. Rename R2 bucket to 'building-images' (recommended)")
        print("   2. Update .env: R2_BUCKET=building_images")

    print("\n✅ Script complete!")


if __name__ == "__main__":
    main()
