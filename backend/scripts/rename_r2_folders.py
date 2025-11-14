# scripts/rename_r2_folders.py
import boto3
import os
import psycopg2

R2_ENDPOINT = os.getenv('R2_ENDPOINT')
R2_ACCESS_KEY = os.getenv('R2_ACCESS_KEY')
R2_SECRET_KEY = os.getenv('R2_SECRET_KEY')
SCAN_DB_URL = os.getenv('SCAN_DB_URL')

s3 = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY
)

# BBL mappings to fix
renames = [
    ('7953318554', '1012660001'),  # 630 Fifth Ave
    ('1014120062', '3020230150'),  # Hospital Road
    ('1005230048', '1005357501'),  # 693-697 Broadway
    ('1012747505', '1012747504'),  # 768 Fifth Ave
]

bucket = 'building-images'

for old_bbl, new_bbl in renames:
    print(f"\nRenaming {old_bbl} → {new_bbl}")
    
    # List all objects in old folder
    response = s3.list_objects_v2(Bucket=bucket, Prefix=f"{old_bbl}/")
    
    if 'Contents' not in response:
        print(f"  ⚠️  No files found for {old_bbl}")
        continue
    
    files = response['Contents']
    print(f"  Found {len(files)} files")
    
    # Copy each file to new location
    for obj in files:
        old_key = obj['Key']
        new_key = old_key.replace(f"{old_bbl}/", f"{new_bbl}/")
        
        # Copy
        s3.copy_object(
            Bucket=bucket,
            CopySource={'Bucket': bucket, 'Key': old_key},
            Key=new_key
        )
        print(f"    Copied: {old_key} → {new_key}")
        
        # Delete old
        s3.delete_object(Bucket=bucket, Key=old_key)
    
    print(f"  ✅ Renamed folder")

# Update database image_key paths
print("\n" + "="*60)
print("Updating database image_key paths...")
print("="*60)

conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

for old_bbl, new_bbl in renames:
    cur.execute("""
        UPDATE reference_embeddings
        SET image_key = REPLACE(image_key, %s, %s)
        WHERE image_key LIKE %s
    """, (f"{old_bbl}/", f"{new_bbl}/", f"{old_bbl}/%"))
    
    rows = cur.rowcount
    print(f"  Updated {rows} embeddings: {old_bbl} → {new_bbl}")

conn.commit()
conn.close()

print("\n✅ All R2 folders renamed and database updated!")