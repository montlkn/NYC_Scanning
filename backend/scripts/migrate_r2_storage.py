#!/usr/bin/env python3
"""
Script to migrate R2 storage from BBL-based paths to BIN-based paths.

Current structure: reference/{bbl}/{angle}.jpg
New structure:     reference/{bin}/{angle}.jpg

This script:
1. Lists all objects in the reference/ prefix
2. For each object with BBL path, finds the corresponding BIN
3. Copies to new BIN path
4. Deletes old path when copy is verified
5. Reports progress and any errors
"""

import asyncio
from typing import List, Dict, Optional, Tuple
import logging
from datetime import datetime
import json

# These imports would need to be uncommented with actual R2 client
# import boto3
# from botocore.exceptions import ClientError
# from sqlalchemy.ext.asyncio import AsyncSession
# from sqlalchemy import select
# from models.database import Building
# from models.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class R2StorageMigrator:
    """Migrate R2 storage from BBL paths to BIN paths."""

    def __init__(
        self,
        r2_account_id: Optional[str] = None,
        r2_access_key: Optional[str] = None,
        r2_secret_key: Optional[str] = None,
        r2_bucket: str = "reference-images",
        dry_run: bool = True
    ):
        """
        Initialize R2 migrator.

        Args:
            r2_account_id: Cloudflare R2 account ID
            r2_access_key: R2 access key
            r2_secret_key: R2 secret access key
            r2_bucket: R2 bucket name
            dry_run: If True, don't actually copy/delete (test mode)
        """
        self.r2_account_id = r2_account_id
        self.r2_access_key = r2_access_key
        self.r2_secret_key = r2_secret_key
        self.r2_bucket = r2_bucket
        self.dry_run = dry_run

        # Uncomment when using actual R2
        # self.s3_client = boto3.client(
        #     's3',
        #     endpoint_url=f'https://{r2_account_id}.r2.cloudflarestorage.com',
        #     aws_access_key_id=r2_access_key,
        #     aws_secret_access_key=r2_secret_key,
        #     region_name='auto'
        # )

        self.migration_stats = {
            'total_objects': 0,
            'skipped_already_migrated': 0,
            'successfully_copied': 0,
            'failed_copies': 0,
            'failed_deletes': 0,
            'errors': []
        }

    def parse_bbl_from_path(self, path: str) -> Optional[str]:
        """
        Extract BBL from R2 path.

        Expected format: reference/{bbl}/{angle}.jpg
        Where BBL = 10 digit number (e.g., 1012890036)

        Args:
            path: Full R2 object path

        Returns:
            BBL string or None if not parseable
        """
        parts = path.split('/')
        if len(parts) >= 3 and parts[0] == 'reference':
            potential_bbl = parts[1]
            # BBL is typically 10 digits
            if potential_bbl.isdigit() and len(potential_bbl) == 10:
                return potential_bbl
        return None

    async def get_bin_for_bbl(self, bbl: str, db_session) -> Optional[str]:
        """
        Look up BIN for a given BBL from database.

        Args:
            bbl: Building BBL to look up
            db_session: Database session

        Returns:
            BIN string or None if not found
        """
        # Uncomment when using actual database
        # try:
        #     result = await db_session.execute(
        #         select(Building.bin).where(Building.bbl == bbl)
        #     )
        #     bin_value = result.scalar_one_or_none()
        #     return bin_value
        # except Exception as e:
        #     logger.error(f"Failed to look up BIN for BBL {bbl}: {e}")
        #     return None

        # Mock implementation for testing
        logger.debug(f"Looking up BIN for BBL {bbl}")
        return None  # Would return actual BIN from database

    async def copy_object(self, source_key: str, dest_key: str) -> bool:
        """
        Copy object from source path to destination path in R2.

        Args:
            source_key: Source object key (BBL-based path)
            dest_key: Destination object key (BIN-based path)

        Returns:
            True if successful, False otherwise
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would copy: {source_key} -> {dest_key}")
            return True

        # Uncomment when using actual R2
        # try:
        #     self.s3_client.copy_object(
        #         Bucket=self.r2_bucket,
        #         CopySource={'Bucket': self.r2_bucket, 'Key': source_key},
        #         Key=dest_key
        #     )
        #     logger.info(f"✅ Copied: {source_key} -> {dest_key}")
        #     return True
        # except ClientError as e:
        #     logger.error(f"❌ Failed to copy {source_key}: {e}")
        #     self.migration_stats['errors'].append({
        #         'path': source_key,
        #         'operation': 'copy',
        #         'error': str(e)
        #     })
        #     return False

        logger.info(f"Would copy: {source_key} -> {dest_key}")
        return True

    async def delete_object(self, key: str) -> bool:
        """
        Delete object from R2.

        Args:
            key: Object key to delete

        Returns:
            True if successful, False otherwise
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would delete: {key}")
            return True

        # Uncomment when using actual R2
        # try:
        #     self.s3_client.delete_object(
        #         Bucket=self.r2_bucket,
        #         Key=key
        #     )
        #     logger.info(f"🗑️  Deleted: {key}")
        #     return True
        # except ClientError as e:
        #     logger.error(f"❌ Failed to delete {key}: {e}")
        #     self.migration_stats['errors'].append({
        #         'path': key,
        #         'operation': 'delete',
        #         'error': str(e)
        #     })
        #     return False

        logger.info(f"Would delete: {key}")
        return True

    async def list_objects_with_prefix(self, prefix: str = "reference/") -> List[str]:
        """
        List all objects in R2 with given prefix.

        Args:
            prefix: Prefix to search (default: reference/)

        Returns:
            List of object keys
        """
        objects = []

        # Uncomment when using actual R2
        # try:
        #     paginator = self.s3_client.get_paginator('list_objects_v2')
        #     pages = paginator.paginate(Bucket=self.r2_bucket, Prefix=prefix)
        #
        #     for page in pages:
        #         if 'Contents' in page:
        #             objects.extend([obj['Key'] for obj in page['Contents']])
        #
        #     logger.info(f"Found {len(objects)} objects with prefix {prefix}")
        #     return objects
        # except ClientError as e:
        #     logger.error(f"Failed to list objects: {e}")
        #     return []

        # Mock implementation
        logger.info(f"[MOCK] Would list objects with prefix: {prefix}")
        return []

    async def migrate(self, db_session=None) -> Dict:
        """
        Execute the full migration from BBL-based paths to BIN-based paths.

        Args:
            db_session: Database session for BIN lookups

        Returns:
            Migration statistics
        """
        logger.info("=" * 80)
        logger.info("R2 STORAGE MIGRATION: BBL → BIN")
        logger.info("=" * 80)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")

        # List all objects in reference/ prefix
        logger.info("\n📋 Scanning R2 storage...")
        objects = await self.list_objects_with_prefix("reference/")
        self.migration_stats['total_objects'] = len(objects)

        if not objects:
            logger.info("No objects found to migrate")
            return self.migration_stats

        logger.info(f"\n🔄 Processing {len(objects)} objects...")

        # Process each object
        for i, obj_key in enumerate(objects, 1):
            logger.info(f"  [{i}/{len(objects)}] Processing: {obj_key}")

            # Check if already migrated (BIN path)
            if obj_key.startswith("reference/") and all(c.isdigit() for c in obj_key.split('/')[1][:7]):
                # Looks like BIN path (shorter, all digits)
                logger.debug(f"  ↳ Already migrated or BIN path")
                self.migration_stats['skipped_already_migrated'] += 1
                continue

            # Extract BBL from path
            bbl = self.parse_bbl_from_path(obj_key)
            if not bbl:
                logger.warning(f"  ⚠️  Could not parse BBL from path: {obj_key}")
                continue

            # Get filename and extension
            filename = obj_key.split('/')[-1]

            # Look up BIN for BBL
            # bin_value = await self.get_bin_for_bbl(bbl, db_session)
            # For now, we'll skip actual lookup
            bin_value = None

            if not bin_value:
                logger.warning(f"  ⚠️  Could not find BIN for BBL {bbl}")
                self.migration_stats['errors'].append({
                    'path': obj_key,
                    'issue': 'BIN not found for BBL',
                    'bbl': bbl
                })
                continue

            # Build new destination path
            new_key = f"reference/{bin_value}/{filename}"

            # Copy to new location
            copy_success = await self.copy_object(obj_key, new_key)
            if not copy_success:
                self.migration_stats['failed_copies'] += 1
                continue

            self.migration_stats['successfully_copied'] += 1

            # Delete old location
            delete_success = await self.delete_object(obj_key)
            if not delete_success:
                self.migration_stats['failed_deletes'] += 1
                logger.warning(f"  ⚠️  Successfully copied but failed to delete old path")

        return self.migration_stats

    def print_report(self):
        """Print migration report."""
        logger.info("\n" + "=" * 80)
        logger.info("MIGRATION REPORT")
        logger.info("=" * 80)

        logger.info(f"\n📊 STATISTICS:")
        logger.info(f"  Total objects scanned:      {self.migration_stats['total_objects']}")
        logger.info(f"  Already migrated:           {self.migration_stats['skipped_already_migrated']}")
        logger.info(f"  Successfully copied:        {self.migration_stats['successfully_copied']}")
        logger.info(f"  Failed copies:              {self.migration_stats['failed_copies']}")
        logger.info(f"  Failed deletes:             {self.migration_stats['failed_deletes']}")

        if self.migration_stats['errors']:
            logger.warning(f"\n⚠️  ERRORS ({len(self.migration_stats['errors'])}):")
            for error in self.migration_stats['errors'][:10]:
                logger.warning(f"  - {error}")
            if len(self.migration_stats['errors']) > 10:
                logger.warning(f"  ... and {len(self.migration_stats['errors']) - 10} more")

        logger.info("\n" + "=" * 80)

    def save_report(self, output_path: str = "r2_migration_report.json"):
        """Save migration report to JSON file."""
        report = {
            'timestamp': datetime.now().isoformat(),
            'mode': 'dry_run' if self.dry_run else 'live',
            'statistics': self.migration_stats
        }

        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)

        logger.info(f"✅ Report saved to: {output_path}")


async def main():
    """Run R2 storage migration."""
    # Configuration
    DRY_RUN = True  # Change to False to actually migrate
    R2_BUCKET = "reference-images"

    # Initialize migrator
    migrator = R2StorageMigrator(
        # r2_account_id=os.getenv('R2_ACCOUNT_ID'),
        # r2_access_key=os.getenv('R2_ACCESS_KEY'),
        # r2_secret_key=os.getenv('R2_SECRET_KEY'),
        r2_bucket=R2_BUCKET,
        dry_run=DRY_RUN
    )

    # Run migration
    try:
        stats = await migrator.migrate()
        migrator.print_report()
        migrator.save_report()

        logger.info("\n✅ Migration process completed")

    except Exception as e:
        logger.error(f"❌ Migration failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
