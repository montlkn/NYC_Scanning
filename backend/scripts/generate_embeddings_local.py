import torch
import open_clip
from PIL import Image
import psycopg2
import boto3
import os
from io import BytesIO
import numpy as np

SCAN_DB_URL = os.getenv('SCAN_DB_URL')
R2_ENDPOINT = f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"

print('Loading CLIP model (CPU)...')
model, _, preprocess = open_clip.create_model_and_transforms('ViT-B-32', pretrained='openai')
model.eval()
print('Model loaded')

s3 = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY')
)

conn = psycopg2.connect(SCAN_DB_URL)
cur = conn.cursor()

# Pre-load all building BBL to ID mappings
cur.execute('SELECT bbl, id FROM buildings')
bbl_to_id = {row[0]: row[1] for row in cur.fetchall()}
print(f'Loaded {len(bbl_to_id)} building mappings')

def encode_image(image_bytes):
    image = Image.open(BytesIO(image_bytes))
    image_tensor = preprocess(image).unsqueeze(0)
    with torch.no_grad():
        embedding = model.encode_image(image_tensor)
    embedding = embedding / embedding.norm(dim=-1, keepdim=True)
    return embedding.cpu().numpy()[0]

print('Fetching images from R2...')
response = s3.list_objects_v2(Bucket=os.getenv('R2_BUCKET'))
objects = response.get('Contents', [])
print(f'Processing {len(objects)} images...')

processed = 0
skipped = 0

for idx, obj in enumerate(objects):
    key = obj['Key']
    parts = key.split('/')
    if len(parts) != 2:
        continue
    
    bbl, filename = parts
    
    # Parse angle and pitch from filename
    try:
        angle = int(filename.split('deg')[0])
        pitch = int(filename.split('_')[1].split('pitch')[0])
    except:
        print(f'Skipping invalid filename: {filename}')
        continue
    
    # Lookup building ID
    building_id = bbl_to_id.get(bbl)
    if not building_id:
        skipped += 1
        if skipped <= 5:
            print(f'Building not found for BBL: {bbl}')
        continue
    
    # Download and encode
    img_data = s3.get_object(Bucket=os.getenv('R2_BUCKET'), Key=key)['Body'].read()
    embedding = encode_image(img_data)
    
    # Insert
    cur.execute("""
        INSERT INTO reference_embeddings 
        (building_id, angle, pitch, embedding, image_key)
        VALUES (%s, %s, %s, %s, %s)
    """, (building_id, angle, pitch, embedding.tolist(), key))
    
    processed += 1
    if (idx + 1) % 50 == 0:
        conn.commit()
        print(f'  {idx + 1}/{len(objects)}')

conn.commit()
print(f'\nProcessed: {processed}')
print(f'Skipped: {skipped}')
print(f'✅ Generated {processed} embeddings')
conn.close()
