#!/usr/bin/env python3
"""
Reorganize R2 storage folders: {bbl}/ → {bin}/

Simpler version using psycopg2 to avoid SQLAlchemy async issues with pgbouncer.
"""

import logging
import re
import sys
import os
from pathlib import Path
from typing import Dict, List
from dotenv import load_dotenv

# Add backend directory to Python path FIRST
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

load_dotenv()

try:
    import psycopg2
    from models.config import get_settings
    from utils.storage import s3_client
except ImportError as e:
    print(f"❌ Missing dependency: {e}")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class R2FolderReorganizer:
    """Reorganize R2 from BBL-based to BIN-based folder structure"""

    def __init__(self, database_url: str, dry_run: bool = True):
        self.database_url = database_url
        self.dry_run = dry_run
        self.conn = None
        self.stats = {
            'total_objects': 0,
            'copied': 0,
            'deleted': 0,
            'errors': 0,
            'skipped': 0,
            'bbl_to_bin_map': {}
        }

    def connect_db(self):
        """Connect to database using psycopg2"""
        try:
            self.conn = psycopg2.connect(self.database_url)
            logger.info("Connected to database")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise

    def disconnect_db(self):
        """Disconnect from database"""
        if self.conn:
            self.conn.close()

    def build_bbl_to_bin_map(self) -> Dict[str, str]:
        """Build mapping of BBL → BIN from database"""
        logger.info("Building BBL → BIN mapping from database...")

        mapping = {}

        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT bbl, bin FROM public.buildings_full_merge_scanning WHERE bbl IS NOT NULL AND bin IS NOT NULL")

            for bbl, bin_val in cursor.fetchall():
                if bbl and bin_val:
                    # Remove .0 suffix from BBL and BIN (stored as "1012890036.0" in DB)
                    bbl_clean = str(bbl).replace('.0', '')
                    bin_clean = str(bin_val).replace('.0', '')
                    mapping[bbl_clean] = bin_clean

            cursor.close()

        except Exception as e:
            logger.error(f"Failed to build mapping: {e}")
            raise

        logger.info(f"Built mapping: {len(mapping):,} BBL → BIN entries")

        self.stats['bbl_to_bin_map'] = mapping
        return mapping

    def extract_bbl_from_path(self, path: str) -> str:
        """Extract BBL from R2 path format: {bbl}/..."""
        parts = path.split('/')
        if len(parts) >= 1:
            potential_bbl = parts[0]
            # BBL is 10 digits
            if re.match(r'^\d{10}$', potential_bbl):
                return potential_bbl

        return None

    def list_reference_objects(self) -> List[Dict]:
        """List all objects in BBL-based root folders (10-digit folder names)"""
        logger.info("Listing objects in R2 BBL-based folders...")

        settings = get_settings()

        objects = []

        try:
            # Get all BBL folders (CommonPrefixes with 10-digit names)
            response = s3_client.list_objects_v2(
                Bucket=settings.r2_bucket,
                Delimiter='/'
            )

            if 'CommonPrefixes' in response:
                for prefix in response['CommonPrefixes']:
                    folder = prefix['Prefix'].rstrip('/')
                    # Check if folder name is BBL (10 digits)
                    if re.match(r'^\d{10}$', folder):
                        # List all objects in this BBL folder
                        bbl_response = s3_client.list_objects_v2(
                            Bucket=settings.r2_bucket,
                            Prefix=f"{folder}/"
                        )

                        if 'Contents' in bbl_response:
                            for obj in bbl_response['Contents']:
                                objects.append({
                                    'key': obj['Key'],
                                    'size': obj['Size'],
                                    'last_modified': obj['LastModified']
                                })
                                self.stats['total_objects'] += 1

        except Exception as e:
            logger.error(f"Failed to list R2 objects: {e}")
            raise

        logger.info(f"Found {len(objects):,} objects in BBL folders")

        return objects

    def reorganize_objects(self, objects: List[Dict], bbl_to_bin: Dict[str, str]):
        """Reorganize objects from BBL to BIN folder structure"""
        logger.info(f"\nReorganizing {len(objects):,} objects...")
        logger.info(f"Mode: {'DRY RUN (no changes)' if self.dry_run else 'ACTUAL COPY'}")

        settings = get_settings()

        for idx, obj in enumerate(objects):
            try:
                old_key = obj['key']

                # Extract BBL from path
                bbl = self.extract_bbl_from_path(old_key)
                if not bbl:
                    self.stats['skipped'] += 1
                    continue

                # Look up BIN
                bin_val = bbl_to_bin.get(bbl)
                if not bin_val:
                    logger.warning(f"  ⚠️  No BIN found for BBL {bbl}: {old_key}")
                    self.stats['skipped'] += 1
                    continue

                # Build new key (replace BBL folder with BIN folder)
                new_key = old_key.replace(f'{bbl}/', f'{bin_val}/')

                if self.dry_run:
                    # Just report
                    logger.debug(f"  COPY: {old_key} → {new_key}")
                    self.stats['copied'] += 1
                else:
                    # Actually copy
                    try:
                        # Copy object
                        copy_source = {
                            'Bucket': settings.r2_bucket,
                            'Key': old_key
                        }

                        s3_client.copy_object(
                            CopySource=copy_source,
                            Bucket=settings.r2_bucket,
                            Key=new_key
                        )

                        # Delete old
                        s3_client.delete_object(
                            Bucket=settings.r2_bucket,
                            Key=old_key
                        )

                        self.stats['copied'] += 1
                        self.stats['deleted'] += 1

                        if (idx + 1) % 100 == 0:
                            logger.info(f"  Processed {idx + 1:,} / {len(objects):,}")

                    except Exception as e:
                        logger.error(f"  ❌ Error processing {old_key}: {e}")
                        self.stats['errors'] += 1

            except Exception as e:
                logger.error(f"  ❌ Error on object {idx}: {e}")
                self.stats['errors'] += 1

        logger.info(f"✅ Reorganization {'plan' if self.dry_run else 'completed'}")

    def verify_reorganization(self) -> bool:
        """Verify new folder structure"""
        logger.info("\nVerifying new folder structure...")

        settings = get_settings()

        try:
            # Count objects in root folders
            response = s3_client.list_objects_v2(
                Bucket=settings.r2_bucket,
                Delimiter='/'
            )

            if 'CommonPrefixes' not in response:
                logger.error("No folders found in bucket")
                return False

            # Check if using BIN-based paths (at root level)
            bin_based = 0
            bbl_based = 0

            for prefix in response['CommonPrefixes']:
                folder = prefix['Prefix'].rstrip('/')
                # Check if looks like BIN (7 digits or less) vs BBL (exactly 10 digits)
                if re.match(r'^\d{10}$', folder):
                    bbl_based += 1
                elif re.match(r'^\d+$', folder):
                    bin_based += 1

            logger.info(f"BIN-based folders: {bin_based}")
            logger.info(f"BBL-based folders: {bbl_based}")

            if bbl_based == 0:
                logger.info("✅ All folders are BIN-based")
                return True
            else:
                logger.warning(f"⚠️  Still found {bbl_based} BBL-based folders")
                return False

        except Exception as e:
            logger.error(f"Failed to verify: {e}")
            return False

    def generate_report(self):
        """Generate reorganization report"""
        logger.info("\n" + "="*70)
        logger.info("R2 FOLDER REORGANIZATION REPORT")
        logger.info("="*70)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'ACTUAL'}")
        logger.info(f"Total Objects Found: {self.stats['total_objects']:,}")
        logger.info(f"Copied: {self.stats['copied']:,}")
        logger.info(f"Deleted: {self.stats['deleted']:,}")
        logger.info(f"Skipped: {self.stats['skipped']:,}")
        logger.info(f"Errors: {self.stats['errors']}")
        logger.info("="*70)

        if self.stats['errors'] == 0:
            logger.info("✅ REORGANIZATION SUCCESSFUL")
        else:
            logger.warning(f"⚠️  {self.stats['errors']} errors during reorganization")

    def run(self):
        """Execute reorganization"""
        try:
            self.connect_db()

            # Step 1: Build mapping
            logger.info("[1/4] Building BBL → BIN mapping...")
            bbl_to_bin = self.build_bbl_to_bin_map()

            # Step 2: List objects
            logger.info("[2/4] Listing R2 objects...")
            objects = self.list_reference_objects()

            # Step 3: Reorganize
            logger.info("[3/4] Reorganizing folder structure...")
            self.reorganize_objects(objects, bbl_to_bin)

            # Step 4: Verify
            logger.info("[4/4] Verifying reorganization...")
            self.verify_reorganization()

            self.generate_report()

            self.disconnect_db()

            return 0

        except Exception as e:
            logger.error(f"FATAL ERROR: {e}", exc_info=True)
            self.disconnect_db()
            return 1


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Reorganize R2 folders from BBL to BIN structure"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Don't actually move files (default: true)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually move files (requires explicit flag)"
    )

    args = parser.parse_args()

    # Override dry_run based on flags
    dry_run = not args.execute

    settings = get_settings()
    db_url = settings.database_url

    logger.info("🚀 R2 Folder Reorganization: BBL → BIN")
    logger.info(f"Bucket: {settings.r2_bucket}")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'ACTUAL EXECUTION'}")

    reorganizer = R2FolderReorganizer(
        database_url=db_url,
        dry_run=dry_run
    )

    return reorganizer.run()


if __name__ == '__main__':
    exit_code = main()
    exit(exit_code)
