#!/usr/bin/env python3
"""
Tax Photo Post-Processor
------------------------
1. Auto-Crop: Removes black borders and metadata text from tax photos.
2. Structure: Keeps original in 'raw/' folder, puts cropped in main folder.
"""

import os
import cv2
import numpy as np
from pathlib import Path
import shutil
from tqdm import tqdm
import argparse

def auto_crop_image(image_path, output_path):
    try:
        # Read image
        img = cv2.imread(str(image_path))
        if img is None:
            return False

        # Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Threshold to separate image from black borders
        # Tax photos usually have very black borders (near 0)
        _, thresh = cv2.threshold(gray, 20, 255, cv2.THRESH_BINARY)

        # Find contours
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            return False

        # Find the largest contour (the actual photo)
        max_area = 0
        best_rect = (0, 0, img.shape[1], img.shape[0])

        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            
            # Filter out small noise
            if area > max_area and area > (img.shape[0] * img.shape[1] * 0.1):
                max_area = area
                best_rect = (x, y, w, h)

        # Crop
        x, y, w, h = best_rect
        
        # Add a small safety padding (optional, keeps a bit of context)
        # x = max(0, x - 5)
        # y = max(0, y - 5)
        # w = min(img.shape[1] - x, w + 10)
        # h = min(img.shape[0] - y, h + 10)

        cropped = img[y:y+h, x:x+w]

        # Save
        cv2.imwrite(str(output_path), cropped)
        return True

    except Exception as e:
        print(f"Error processing {image_path}: {e}")
        return False

def process_directory(base_dir):
    base_path = Path(base_dir)
    images = list(base_path.rglob("*.jpg"))
    
    print(f"Found {len(images)} images in {base_dir}")
    
    success = 0
    skipped = 0
    
    for img_path in tqdm(images):
        if "raw_backup" in str(img_path):
            continue
            
        # Define paths
        folder = img_path.parent
        backup_folder = folder / "raw_backup"
        backup_folder.mkdir(exist_ok=True)
        
        backup_path = backup_folder / img_path.name
        
        # Check if already processed
        if backup_path.exists():
            skipped += 1
            continue
            
        # Copy original to backup
        shutil.copy2(img_path, backup_path)
        
        # Crop and overwrite original (or save as new name if you prefer)
        if auto_crop_image(backup_path, img_path):
            success += 1
        else:
            # If crop fails, restore original
            shutil.copy2(backup_path, img_path)

    print(f"Processed: {success}")
    print(f"Skipped (already done): {skipped}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Directory to process")
    args = parser.parse_args()
    
    process_directory(args.dir)
