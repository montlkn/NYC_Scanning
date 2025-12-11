#!/usr/bin/env python3
"""
Simple R2 bucket structure checker (standalone, reads .env directly)
"""

import boto3
from botocore.client import Config
import os
from pathlib import Path

# Load .env file
env_path = Path(__file__).parent.parent / '.env'

if not env_path.exists():
    print(f"❌ Error: .env file not found at {env_path}")
    print(f"   Please create backend/.env with your R2 credentials")
    exit(1)

# Parse .env manually
env_vars = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            env_vars[key.strip()] = value.strip().strip('"').strip("'")

# Get R2 credentials
r2_account_id = env_vars.get('R2_ACCOUNT_ID')
r2_access_key = env_vars.get('R2_ACCESS_KEY_ID')
r2_secret_key = env_vars.get('R2_SECRET_ACCESS_KEY')
r2_bucket = env_vars.get('R2_BUCKET', 'building-images')
r2_public_url = env_vars.get('R2_PUBLIC_URL', '')

if not all([r2_account_id, r2_access_key, r2_secret_key]):
    print("❌ Missing R2 credentials in .env file")
    print("   Required: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY")
    exit(1)

print(f"\n{'=' * 80}")
print("CLOUDFLARE R2 STRUCTURE CHECK")
print(f"{'=' * 80}\n")

print(f"Config:")
print(f"  Account ID: {r2_account_id[:8]}...")
print(f"  Bucket: {r2_bucket}")
print(f"  Public URL: {r2_public_url}\n")

# Initialize S3 client
try:
    s3 = boto3.client(
        's3',
        endpoint_url=f'https://{r2_account_id}.r2.cloudflarestorage.com',
        aws_access_key_id=r2_access_key,
        aws_secret_access_key=r2_secret_key,
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )
    print("✅ R2 client initialized\n")
except Exception as e:
    print(f"❌ Failed to initialize R2 client: {e}")
    exit(1)

# Skip bucket listing (requires admin permissions)
# Go straight to analyzing the configured bucket
bucket_to_analyze = r2_bucket
print(f"Using configured bucket: {bucket_to_analyze}\n")

# Analyze bucket structure
print(f"\n{'=' * 80}")
print(f"BUCKET ANALYSIS: {bucket_to_analyze}")
print(f"{'=' * 80}\n")

try:
    # List all objects
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=bucket_to_analyze, MaxKeys=1000)

    all_keys = []
    total_size = 0

    for page in pages:
        if 'Contents' in page:
            for obj in page['Contents']:
                all_keys.append({
                    'key': obj['Key'],
                    'size': obj['Size'],
                    'modified': obj['LastModified']
                })
                total_size += obj['Size']

    print(f"📊 Total objects: {len(all_keys)}")
    print(f"💾 Total size: {total_size / (1024*1024):.2f} MB\n")

    if not all_keys:
        print("⚠️  Bucket is empty - no objects found")
        print("\n   This is normal if you haven't uploaded any images yet.")
        print("   Once you start scanning buildings, files will appear here.")
        exit(0)

    # Analyze directory structure
    prefixes = {}
    for obj in all_keys:
        key = obj['key']
        if '/' in key:
            prefix = key.split('/')[0]
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        else:
            prefixes['(root)'] = prefixes.get('(root)', 0) + 1

    print("📁 Directory structure:")
    for prefix, count in sorted(prefixes.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"   {prefix}/  ({count} objects)")

    # Sample keys
    print(f"\n📄 Sample objects (first 15):")
    for obj in sorted(all_keys, key=lambda x: x['modified'], reverse=True)[:15]:
        size_kb = obj['size'] / 1024
        key = obj['key']
        if len(key) > 60:
            key = key[:57] + "..."
        print(f"   {key}")
        print(f"      {size_kb:.1f} KB, {obj['modified'].strftime('%Y-%m-%d %H:%M')}")

    if len(all_keys) > 15:
        print(f"   ... and {len(all_keys) - 15} more")

    # Check expected structure
    print(f"\n🔍 Expected directories:")
    expected = {
        'scans': 'User scan uploads (from /api/scan)',
        'user-images': 'Confirmed user contributions',
        'reference': 'Street View reference images',
        'building-images': 'Old structure (deprecated)'
    }

    for dir_name, description in expected.items():
        count = sum(1 for obj in all_keys if obj['key'].startswith(f"{dir_name}/"))
        status = "✅" if count > 0 else "❌"
        print(f"   {status} {dir_name}/  ({count} objects)")
        if count > 0:
            print(f"      → {description}")

    print(f"\n{'=' * 80}")
    print("RECOMMENDATIONS")
    print(f"{'=' * 80}\n")

    if r2_bucket != bucket_to_analyze:
        print(f"⚠️  Bucket name mismatch!")
        print(f"   Config expects: '{r2_bucket}'")
        print(f"   Actual bucket: '{bucket_to_analyze}'")
        print(f"\n   Fix: Update .env file:")
        print(f"   R2_BUCKET={bucket_to_analyze}")

    if any('building-images/' in obj['key'] for obj in all_keys):
        print(f"\n⚠️  Found 'building-images/' prefix in keys")
        print(f"   This suggests nested structure instead of flat")
        print(f"   Example: building-images/scans/123.jpg")
        print(f"   Should be: scans/123.jpg")
        print(f"\n   This won't break anything but wastes one directory level")

    print("\n✅ R2 structure check complete!")

except Exception as e:
    print(f"❌ Error analyzing bucket: {e}")
    import traceback
    traceback.print_exc()
