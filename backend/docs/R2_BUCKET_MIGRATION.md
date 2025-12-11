# R2 Bucket Migration Guide

## Step 1: Create the `user-images` Bucket

### In Cloudflare Dashboard:

1. Go to **R2 Object Storage** in your Cloudflare dashboard
2. Click **Create bucket**
3. Enter bucket name: `user-images`
4. Select the same region as your `building-images` bucket
5. Click **Create bucket**

### Configure Public Access:

1. Click on the newly created `user-images` bucket
2. Go to **Settings** tab
3. Scroll to **Public Access**
4. Click **Allow Access** (this creates the public R2.dev URL)
5. Copy the public URL (should be: `https://pub-234fc67c039149b2b46b864a1357763d.r2.dev`)

### Configure CORS (if needed):

If your app needs direct browser uploads to this bucket, add CORS policy:

```json
[
  {
    "AllowedOrigins": [
      "https://jink.city",
      "https://*.jink.city"
    ],
    "AllowedMethods": [
      "GET",
      "HEAD",
      "PUT"
    ],
    "AllowedHeaders": [
      "*"
    ],
    "ExposeHeaders": [
      "ETag",
      "Content-Length"
    ],
    "MaxAgeSeconds": 3600
  }
]
```

## Step 2: Update Environment Variables

Your `.env` should already have these (verify):

```bash
# User Images Bucket (separate bucket for user-submitted photos)
R2_USER_IMAGES_BUCKET=user-images
R2_USER_IMAGES_PUBLIC_URL=https://pub-234fc67c039149b2b46b864a1357763d.r2.dev
```

**Note**: The public URL might be the same as building-images bucket, or it might be different. Copy the exact URL from Cloudflare dashboard.

## Step 3: Test the Connection

Run a simple test to verify the bucket is accessible:

```bash
cd /Users/lucienmount/coding/nyc_scan
source venv/bin/activate
python -c "
import boto3
from botocore.client import Config
from models.config import get_settings

settings = get_settings()

s3 = boto3.client(
    's3',
    endpoint_url=f'https://{settings.r2_account_id}.r2.cloudflarestorage.com',
    aws_access_key_id=settings.r2_access_key_id,
    aws_secret_access_key=settings.r2_secret_access_key,
    config=Config(signature_version='s3v4'),
    region_name='auto'
)

try:
    s3.head_bucket(Bucket=settings.r2_user_images_bucket)
    print(f'✓ Successfully connected to {settings.r2_user_images_bucket}')
except Exception as e:
    print(f'✗ Failed to connect: {e}')
"
```

## Step 4: Migrate Existing User Images (Optional)

If you have existing user images in `building-images/user-images/`, migrate them:

### Dry Run First (Recommended)

See what would be migrated without making changes:

```bash
python backend/scripts/migrate_user_images_to_bucket.py --dry-run
```

### Perform Migration

Copy images to new bucket (keeps originals):

```bash
python backend/scripts/migrate_user_images_to_bucket.py
```

### Migrate and Delete Old Images

Copy images and delete from old location:

```bash
python backend/scripts/migrate_user_images_to_bucket.py --delete-old
```

**Warning**: Only use `--delete-old` after verifying the migration was successful and the new images are accessible.

## Step 5: Verify Migration

Check that images are in the new bucket:

```bash
python backend/scripts/check_r2_simple.py
```

This will show:
- Object count in building-images (should decrease if you used --delete-old)
- Folder structure in both buckets

## Step 6: Test Upload Flow

Test that new uploads go to the correct bucket:

1. Take a photo in the app
2. Scan a building
3. Confirm the building match
4. Check Cloudflare R2 dashboard:
   - `user-images/{user_id}/{BIN}/` should have the new photo
   - Filename format: `{scan_id}_{bearing}deg_{timestamp}.jpg`

## Step 7: Update Database References (If Needed)

If you had existing user images with embeddings in `reference_embeddings`, update their `image_key` to point to the new bucket:

```sql
-- Check current image_keys
SELECT image_key, COUNT(*)
FROM reference_embeddings
WHERE image_key LIKE 'user-images/%'
GROUP BY image_key;

-- Update image_keys to remove 'user-images/' prefix if they were in old location
-- (Only run this if you migrated images and verified they work)
UPDATE reference_embeddings
SET image_key = REPLACE(image_key, 'user-images/', '')
WHERE image_key LIKE 'user-images/%';
```

**Note**: Only run the UPDATE if your images were stored with the full `user-images/` prefix in the image_key. Most likely they were stored without the bucket name prefix, so this step may not be needed.

## Step 8: Configure Lifecycle Rules (Optional)

Set up automatic deletion of temporary scans after 24 hours:

### In Cloudflare Dashboard:

1. Go to **building-images** bucket
2. Go to **Settings** → **Lifecycle rules**
3. Click **Create lifecycle rule**
4. Configure:
   - **Rule name**: Delete temporary scans
   - **Apply to prefix**: `scans/`
   - **Action**: Delete objects
   - **After**: 1 day
5. Save

This will automatically clean up temporary scan uploads that weren't confirmed.

## Rollback Plan

If something goes wrong during migration:

### If images were copied but not deleted:
- No action needed, originals still exist in old location
- Delete from new bucket if needed: `aws s3 rm s3://user-images/ --recursive --endpoint-url ...`

### If images were deleted (--delete-old):
- Check if R2 versioning was enabled (Cloudflare keeps deleted objects for 30 days)
- Contact Cloudflare support to restore if needed
- **Best practice**: Always test with `--dry-run` first and verify before using `--delete-old`

## Verification Checklist

- [ ] user-images bucket created in Cloudflare
- [ ] Public access enabled on user-images bucket
- [ ] R2_USER_IMAGES_PUBLIC_URL updated in .env (if different)
- [ ] Connection test passes
- [ ] Migration dry-run shows correct paths
- [ ] Migration completed successfully
- [ ] New uploads go to correct bucket structure
- [ ] Images are publicly accessible via R2.dev URL
- [ ] Database image_key references are correct
- [ ] Lifecycle rules configured for scans/ folder (optional)

## Expected Final Structure

### building-images bucket:
```
scans/                              # Temporary (24h TTL)
  {uuid}.jpg
  {uuid}_thumb.jpg

{BIN}/                              # Reference images (permanent)
  0deg_0pitch.jpg
  90deg_0pitch.jpg
  ...
```

### user-images bucket:
```
{user_id}/                          # User folder
  {BIN}/                            # Building folder
    {scan_id}_90deg_20251203_143022.jpg
    {scan_id}_90deg_20251203_143022_thumb.jpg
    {scan_id}_180deg_20251203_150133.jpg
    ...

anonymous/                          # For non-logged-in users
  {BIN}/
    ...
```

## Troubleshooting

### "Bucket does not exist" error
- Verify bucket name is exactly `user-images` (case-sensitive)
- Check you're using the correct R2 account ID
- Ensure the bucket was created in the same Cloudflare account

### "Access Denied" error
- Verify R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY in .env
- Check the API token has R2 read/write permissions
- Ensure you're not using a read-only token

### Images not visible in app
- Verify R2_USER_IMAGES_PUBLIC_URL is correct
- Check bucket has public access enabled
- Test direct URL access in browser: `{PUBLIC_URL}/{user_id}/{BIN}/{filename}`

### Migration script hangs
- Check internet connection
- Verify R2 account credentials
- Try with --dry-run first to test connection
