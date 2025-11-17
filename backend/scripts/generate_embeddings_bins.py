#!/usr/bin/env python3
"""
Generate CLIP embeddings for reference images using BIN-based folder structure.
"""

import torch
import open_clip
from PIL import Image
import psycopg2
import os
import sys
from io import BytesIO
import numpy as np
from pathlib import Path

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from models.config import get_settings
from utils.storage import s3_client
from dotenv import load_dotenv

load_dotenv()

def main():
    settings = get_settings()

    print('Loading CLIP model (ViT-B-32)...')
    model, _, preprocess = open_clip.create_model_and_transforms('ViT-B-32', pretrained='openai')
    model.eval()
    print('✓ Model loaded')

    # Connect to database
    conn = psycopg2.connect(settings.database_url)
    cur = conn.cursor()

    # Pre-load all building BIN to building_id mappings
    print('\nLoading building BIN mappings from database...')
    cur.execute('SELECT id, bin, bbl FROM buildings_full_merge_scanning WHERE bin IS NOT NULL')
    bin_to_building_id = {}
    for building_id, bin_val, bbl in cur.fetchall():
        # Clean the .0 suffix
        bin_clean = str(bin_val).replace('.0', '')
        bin_to_building_id[bin_clean] = building_id

    print(f'✓ Loaded {len(bin_to_building_id)} building BIN → building_id mappings')

    # Fetch images from R2
    print('\nFetching images from R2...')

    # List all objects in bucket
    all_objects = []
    continuation_token = None

    while True:
        if continuation_token:
            response = s3_client.list_objects_v2(
                Bucket=settings.r2_bucket,
                ContinuationToken=continuation_token
            )
        else:
            response = s3_client.list_objects_v2(Bucket=settings.r2_bucket)

        if 'Contents' in response:
            all_objects.extend(response['Contents'])

        if response.get('IsTruncated'):
            continuation_token = response.get('NextContinuationToken')
        else:
            break

    print(f'✓ Found {len(all_objects)} total objects in R2')

    # Filter for reference images (BIN folders with numeric names)
    reference_images = []
    for obj in all_objects:
        key = obj['Key']
        parts = key.split('/')

        # Must be in format: {BIN}/{filename}
        if len(parts) != 2:
            continue

        bin_folder, filename = parts

        # Skip archive folder
        if bin_folder == 'archive':
            continue

        # Must be numeric BIN folder
        if not bin_folder.isdigit():
            continue

        # Must be a .jpg file with angle/pitch format
        if not filename.endswith('.jpg'):
            continue

        reference_images.append(obj)

    print(f'✓ Found {len(reference_images)} reference images to process')

    def encode_image(image_bytes):
        """Generate CLIP embedding for an image"""
        image = Image.open(BytesIO(image_bytes))
        image_tensor = preprocess(image).unsqueeze(0)
        with torch.no_grad():
            embedding = model.encode_image(image_tensor)
        # Normalize
        embedding = embedding / embedding.norm(dim=-1, keepdim=True)
        return embedding.cpu().numpy()[0]

    print('\nGenerating embeddings...')
    processed = 0
    skipped = 0
    errors = 0

    for idx, obj in enumerate(reference_images):
        key = obj['Key']
        parts = key.split('/')
        bin_folder, filename = parts

        # Parse angle and pitch from filename (e.g., "0deg_0pitch.jpg")
        try:
            angle = int(filename.split('deg')[0])
            pitch = int(filename.split('_')[1].split('pitch')[0])
        except Exception as e:
            print(f'  ⚠️  Skipping invalid filename: {filename} - {e}')
            skipped += 1
            continue

        # Check if BIN exists in our mappings
        if bin_folder not in bin_to_building_id:
            if skipped < 5:  # Only print first few
                print(f'  ⚠️  BIN not found in database: {bin_folder}')
            skipped += 1
            continue

        building_id = bin_to_building_id[bin_folder]

        try:
            # Download image
            img_data = s3_client.get_object(
                Bucket=settings.r2_bucket,
                Key=key
            )['Body'].read()

            # Generate embedding
            embedding = encode_image(img_data)

            # Check if embedding already exists
            cur.execute("""
                SELECT id FROM reference_embeddings
                WHERE building_id = %s AND angle = %s AND pitch = %s
            """, (building_id, angle, pitch))

            existing = cur.fetchone()

            if existing:
                # Update existing
                cur.execute("""
                    UPDATE reference_embeddings
                    SET embedding = %s, image_key = %s
                    WHERE building_id = %s AND angle = %s AND pitch = %s
                """, (embedding.tolist(), key, building_id, angle, pitch))
            else:
                # Insert new
                cur.execute("""
                    INSERT INTO reference_embeddings
                    (building_id, angle, pitch, embedding, image_key)
                    VALUES (%s, %s, %s, %s, %s)
                """, (building_id, angle, pitch, embedding.tolist(), key))

            processed += 1

            if (idx + 1) % 100 == 0:
                conn.commit()
                print(f'  Processed {idx + 1}/{len(reference_images)} ({processed} successful, {skipped} skipped, {errors} errors)')

        except Exception as e:
            errors += 1
            if errors < 5:  # Only print first few errors
                print(f'  ❌ Error processing {key}: {e}')
            continue

    conn.commit()

    print('\n' + '='*70)
    print('EMBEDDING GENERATION COMPLETE')
    print('='*70)
    print(f'Total images processed: {processed}')
    print(f'Skipped (BIN not in DB): {skipped}')
    print(f'Errors: {errors}')
    print(f'✅ Successfully generated {processed} embeddings')
    print('='*70)

    cur.close()
    conn.close()

if __name__ == '__main__':
    main()
