#!/usr/bin/env python3
"""
Archive old BBL folders to tidy up Cloudflare R2 storage.

Moves all BBL-based folders (10-digit names) to an 'archive/' prefix.
This keeps the old data for reference but cleans up the main bucket.
"""

import logging
import re
import sys
from pathlib import Path

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from models.config import get_settings
from utils.storage import s3_client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BBLArchiver:
    """Archive BBL-based folders to archive/ prefix"""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.settings = get_settings()
        self.stats = {
            'total_folders': 0,
            'total_objects': 0,
            'moved': 0,
            'deleted': 0,
            'errors': 0
        }

    def list_bbl_folders(self):
        """List all BBL-based folders (10-digit folder names)"""
        logger.info("Listing BBL-based folders...")

        bbl_folders = []

        try:
            response = s3_client.list_objects_v2(
                Bucket=self.settings.r2_bucket,
                Delimiter='/'
            )

            if 'CommonPrefixes' in response:
                for prefix in response['CommonPrefixes']:
                    folder = prefix['Prefix'].rstrip('/')
                    # Check if folder name is BBL (exactly 10 digits)
                    if re.match(r'^\d{10}$', folder):
                        bbl_folders.append(folder)
                        self.stats['total_folders'] += 1

        except Exception as e:
            logger.error(f"Failed to list R2 folders: {e}")
            raise

        logger.info(f"Found {len(bbl_folders)} BBL-based folders to archive")
        return bbl_folders

    def archive_folder(self, bbl: str):
        """Archive all objects in a BBL folder to archive/ prefix"""
        logger.info(f"Archiving BBL folder: {bbl}")

        try:
            # List all objects in this BBL folder
            response = s3_client.list_objects_v2(
                Bucket=self.settings.r2_bucket,
                Prefix=f"{bbl}/"
            )

            if 'Contents' not in response:
                logger.warning(f"  No objects found in {bbl}/")
                return 0

            objects = response['Contents']
            logger.info(f"  Found {len(objects)} objects to archive")

            moved_count = 0
            for obj in objects:
                old_key = obj['Key']
                # Move to archive/ prefix
                new_key = f"archive/{old_key}"

                if self.dry_run:
                    logger.debug(f"  MOVE: {old_key} → {new_key}")
                    moved_count += 1
                else:
                    try:
                        # Copy to new location
                        copy_source = {
                            'Bucket': self.settings.r2_bucket,
                            'Key': old_key
                        }
                        s3_client.copy_object(
                            CopySource=copy_source,
                            Bucket=self.settings.r2_bucket,
                            Key=new_key
                        )

                        # Delete original
                        s3_client.delete_object(
                            Bucket=self.settings.r2_bucket,
                            Key=old_key
                        )

                        moved_count += 1
                        self.stats['moved'] += 1
                        self.stats['deleted'] += 1

                    except Exception as e:
                        logger.error(f"  ❌ Error moving {old_key}: {e}")
                        self.stats['errors'] += 1

            self.stats['total_objects'] += len(objects)
            logger.info(f"  ✓ Archived {moved_count}/{len(objects)} objects")
            return moved_count

        except Exception as e:
            logger.error(f"Failed to archive folder {bbl}: {e}")
            self.stats['errors'] += 1
            return 0

    def verify_archive(self):
        """Verify archive was successful"""
        logger.info("\nVerifying archive...")

        try:
            # Count BBL folders still at root
            response = s3_client.list_objects_v2(
                Bucket=self.settings.r2_bucket,
                Delimiter='/'
            )

            remaining_bbl = 0
            if 'CommonPrefixes' in response:
                for prefix in response['CommonPrefixes']:
                    folder = prefix['Prefix'].rstrip('/')
                    if re.match(r'^\d{10}$', folder):
                        remaining_bbl += 1

            # Count archived folders
            archive_response = s3_client.list_objects_v2(
                Bucket=self.settings.r2_bucket,
                Prefix='archive/',
                Delimiter='/'
            )

            archived_count = 0
            if 'CommonPrefixes' in archive_response:
                archived_count = len(archive_response['CommonPrefixes'])

            logger.info(f"BBL folders remaining at root: {remaining_bbl}")
            logger.info(f"Folders in archive/: {archived_count}")

            if remaining_bbl == 0:
                logger.info("✅ All BBL folders successfully archived")
                return True
            else:
                logger.warning(f"⚠️  Still found {remaining_bbl} BBL folders at root")
                return False

        except Exception as e:
            logger.error(f"Failed to verify archive: {e}")
            return False

    def generate_report(self):
        """Generate archiving report"""
        logger.info("\n" + "="*70)
        logger.info("BBL FOLDER ARCHIVING REPORT")
        logger.info("="*70)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'ACTUAL'}")
        logger.info(f"BBL Folders Found: {self.stats['total_folders']}")
        logger.info(f"Total Objects: {self.stats['total_objects']}")
        logger.info(f"Objects Moved: {self.stats['moved']}")
        logger.info(f"Objects Deleted (from original): {self.stats['deleted']}")
        logger.info(f"Errors: {self.stats['errors']}")
        logger.info("="*70)

        if self.stats['errors'] == 0:
            logger.info("✅ ARCHIVING SUCCESSFUL")
        else:
            logger.warning(f"⚠️  {self.stats['errors']} errors during archiving")

    def run(self):
        """Execute archiving"""
        try:
            # Step 1: List BBL folders
            logger.info("[1/3] Finding BBL folders to archive...")
            bbl_folders = self.list_bbl_folders()

            if not bbl_folders:
                logger.info("No BBL folders found to archive")
                return 0

            # Step 2: Archive each folder
            logger.info(f"[2/3] Archiving {len(bbl_folders)} folders...")
            logger.info(f"Mode: {'DRY RUN (no changes)' if self.dry_run else 'ACTUAL MOVE'}\n")

            for idx, bbl in enumerate(bbl_folders, 1):
                logger.info(f"[{idx}/{len(bbl_folders)}] {bbl}")
                self.archive_folder(bbl)

            # Step 3: Verify
            logger.info("[3/3] Verifying archive...")
            self.verify_archive()

            self.generate_report()

            return 0

        except Exception as e:
            logger.error(f"FATAL ERROR: {e}", exc_info=True)
            return 1


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Archive old BBL folders to archive/ prefix in R2"
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
    dry_run = not args.execute

    logger.info("🗄️  BBL Folder Archiving: Moving to archive/")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'ACTUAL EXECUTION'}")

    archiver = BBLArchiver(dry_run=dry_run)
    return archiver.run()


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
