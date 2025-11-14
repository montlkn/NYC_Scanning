#!/usr/bin/env python3
"""
Verify BBL to BIN migration completion and data integrity

This script checks:
1. Database schema changes are applied
2. Primary key is BIN not BBL
3. All buildings have valid BINs (except public spaces)
4. Reference images link to buildings by BIN
5. No orphaned data
"""

import asyncio
import logging
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, text, inspect
from models.database import Base, Building, ReferenceImage, Scan

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MigrationVerifier:
    """Verify BIN migration is complete and correct"""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = None
        self.results = {
            'passed': [],
            'failed': [],
            'warnings': [],
            'stats': {}
        }

    async def connect(self):
        """Create database connection"""
        self.engine = create_async_engine(self.database_url, echo=False)
        logger.info("Connected to database")

    async def disconnect(self):
        """Close database connection"""
        if self.engine:
            await self.engine.dispose()

    async def verify_schema(self):
        """Verify database schema changes"""
        logger.info("\n=== Verifying Database Schema ===")

        async with AsyncSession(self.engine) as session:
            # Check primary key
            result = await session.execute(
                text("""
                    SELECT column_name
                    FROM information_schema.table_constraints t
                    JOIN information_schema.key_column_usage k
                        ON t.constraint_name = k.constraint_name
                    WHERE t.table_name = 'buildings_full_merge_scanning'
                    AND t.constraint_type = 'PRIMARY KEY'
                """)
            )

            pk_columns = [row[0] for row in result.fetchall()]

            if 'bin' in pk_columns:
                logger.info("✅ Primary key: BIN (correct)")
                self.results['passed'].append("Primary key is BIN")
            else:
                logger.error("❌ Primary key: NOT BIN!")
                self.results['failed'].append("Primary key is not BIN")
                return False

            # Check BIN column exists
            result = await session.execute(
                text("""
                    SELECT column_name, is_nullable, data_type
                    FROM information_schema.columns
                    WHERE table_name = 'buildings_full_merge_scanning'
                    AND column_name = 'bin'
                """)
            )

            bin_column = result.fetchone()
            if bin_column:
                logger.info(f"✅ BIN column exists: {bin_column[2]}")
                self.results['passed'].append("BIN column exists")
            else:
                logger.error("❌ BIN column not found!")
                self.results['failed'].append("BIN column not found")
                return False

            # Check BBL column exists
            result = await session.execute(
                text("""
                    SELECT column_name, is_nullable, data_type
                    FROM information_schema.columns
                    WHERE table_name = 'buildings_full_merge_scanning'
                    AND column_name = 'bbl'
                """)
            )

            bbl_column = result.fetchone()
            if bbl_column:
                logger.info(f"✅ BBL column exists (secondary): {bbl_column[2]}")
                self.results['passed'].append("BBL column exists as secondary")
            else:
                logger.warning("⚠️  BBL column not found")
                self.results['warnings'].append("BBL column not found")

        return True

    async def verify_data_integrity(self):
        """Verify data integrity"""
        logger.info("\n=== Verifying Data Integrity ===")

        async with AsyncSession(self.engine) as session:
            # Count buildings
            result = await session.execute(
                select(Building).order_by(Building.bin)
            )
            total_buildings = len(result.scalars().all())
            logger.info(f"Total buildings: {total_buildings:,}")
            self.results['stats']['total_buildings'] = total_buildings

            # Buildings with valid BINs
            result = await session.execute(
                text("""
                    SELECT COUNT(*) as count
                    FROM buildings_full_merge_scanning
                    WHERE bin != 'N/A' AND bin IS NOT NULL
                """)
            )
            valid_bins = result.scalar()
            logger.info(f"Buildings with valid BINs: {valid_bins:,} ({valid_bins/total_buildings*100:.2f}%)")
            self.results['stats']['with_valid_bins'] = valid_bins

            if valid_bins >= total_buildings * 0.99:  # 99% coverage
                logger.info("✅ BIN coverage is acceptable (>99%)")
                self.results['passed'].append("BIN coverage > 99%")
            else:
                logger.error(f"❌ BIN coverage is low: {valid_bins/total_buildings*100:.2f}%")
                self.results['failed'].append(f"BIN coverage < 99%: {valid_bins/total_buildings*100:.2f}%")

            # Public spaces
            result = await session.execute(
                text("""
                    SELECT COUNT(*) as count
                    FROM buildings_full_merge_scanning
                    WHERE bin = 'N/A'
                """)
            )
            public_spaces = result.scalar()
            logger.info(f"Public spaces (N/A BIN): {public_spaces:,}")
            self.results['stats']['public_spaces'] = public_spaces

            if public_spaces >= 10:  # At least some public spaces
                logger.info("✅ Public spaces properly marked")
                self.results['passed'].append("Public spaces marked as N/A")
            else:
                logger.warning("⚠️  Few public spaces found")
                self.results['warnings'].append(f"Few public spaces: {public_spaces}")

            # NULL BINs
            result = await session.execute(
                text("""
                    SELECT COUNT(*) as count
                    FROM buildings_full_merge_scanning
                    WHERE bin IS NULL
                """)
            )
            null_bins = result.scalar()
            if null_bins == 0:
                logger.info("✅ No NULL BINs")
                self.results['passed'].append("No NULL BINs")
            else:
                logger.error(f"❌ Found {null_bins} NULL BINs!")
                self.results['failed'].append(f"NULL BINs found: {null_bins}")

    async def verify_foreign_keys(self):
        """Verify foreign key relationships"""
        logger.info("\n=== Verifying Foreign Keys ===")

        async with AsyncSession(self.engine) as session:
            # Check reference images BIN foreign key
            result = await session.execute(
                text("""
                    SELECT constraint_name, table_name, column_name
                    FROM information_schema.key_column_usage
                    WHERE table_name = 'reference_images'
                    AND column_name = 'bin'
                """)
            )

            fk = result.fetchone()
            if fk:
                logger.info(f"✅ Reference images BIN foreign key: {fk[0]}")
                self.results['passed'].append("Reference images FK to building.bin")
            else:
                logger.warning("⚠️  Reference images BIN foreign key not found")
                self.results['warnings'].append("Reference images BIN FK not found")

            # Check for orphaned reference images
            result = await session.execute(
                text("""
                    SELECT COUNT(*) as count
                    FROM reference_images ri
                    WHERE ri.bin NOT IN (
                        SELECT bin FROM buildings_full_merge_scanning
                    )
                """)
            )

            orphaned = result.scalar()
            if orphaned == 0:
                logger.info("✅ No orphaned reference images")
                self.results['passed'].append("No orphaned reference images")
            else:
                logger.error(f"❌ Found {orphaned} orphaned reference images!")
                self.results['failed'].append(f"Orphaned reference images: {orphaned}")

    async def verify_indexes(self):
        """Verify database indexes"""
        logger.info("\n=== Verifying Indexes ===")

        async with AsyncSession(self.engine) as session:
            result = await session.execute(
                text("""
                    SELECT indexname, tablename
                    FROM pg_indexes
                    WHERE tablename IN ('buildings_full_merge_scanning', 'reference_images', 'scans')
                    ORDER BY tablename, indexname
                """)
            )

            indexes = result.fetchall()
            if indexes:
                logger.info(f"✅ Found {len(indexes)} indexes")
                self.results['stats']['indexes'] = len(indexes)
                self.results['passed'].append("Indexes present")

                # Check for important indexes
                index_names = [idx[0] for idx in indexes]

                if any('bin' in name for name in index_names):
                    logger.info("✅ BIN indexes present")
                else:
                    logger.warning("⚠️  BIN indexes not found")
                    self.results['warnings'].append("BIN indexes not found")
            else:
                logger.error("❌ No indexes found!")
                self.results['failed'].append("No indexes found")

    async def verify_sample_data(self):
        """Verify sample data is correct"""
        logger.info("\n=== Verifying Sample Data ===")

        async with AsyncSession(self.engine) as session:
            # Get a building with valid BIN
            result = await session.execute(
                text("""
                    SELECT bin, bbl, address
                    FROM buildings_full_merge_scanning
                    WHERE bin != 'N/A' AND bin IS NOT NULL
                    LIMIT 1
                """)
            )

            building = result.fetchone()
            if building:
                bin_val, bbl_val, address = building
                logger.info(f"✅ Sample building: BIN={bin_val}, BBL={bbl_val}, Address={address[:50]}")

                # Check reference images for this building
                result = await session.execute(
                    text("""
                        SELECT COUNT(*), COUNT(DISTINCT compass_bearing)
                        FROM reference_images
                        WHERE bin = :bin
                    """),
                    {'bin': bin_val}
                )

                count, bearings = result.fetchone()
                if count > 0:
                    logger.info(f"✅ Reference images found: {count} total, {bearings} bearing(s)")

                    # Verify R2 paths use BIN
                    result = await session.execute(
                        text("""
                            SELECT image_url FROM reference_images
                            WHERE bin = :bin LIMIT 1
                        """),
                        {'bin': bin_val}
                    )

                    url = result.scalar()
                    if f"/reference/{bin_val}/" in url:
                        logger.info(f"✅ R2 paths use BIN: {url[:50]}...")
                        self.results['passed'].append("R2 paths use BIN format")
                    else:
                        logger.error(f"❌ R2 path doesn't use BIN: {url}")
                        self.results['failed'].append("R2 paths don't use BIN")
                else:
                    logger.info(f"⚠️  No reference images for sample building")

    async def generate_report(self):
        """Generate verification report"""
        logger.info("\n" + "="*70)
        logger.info("MIGRATION VERIFICATION REPORT")
        logger.info("="*70)

        # Summary
        passed = len(self.results['passed'])
        failed = len(self.results['failed'])
        warnings = len(self.results['warnings'])

        logger.info(f"\n✅ Passed: {passed}")
        logger.info(f"❌ Failed: {failed}")
        logger.info(f"⚠️  Warnings: {warnings}")

        # Details
        if self.results['passed']:
            logger.info("\nPassed Checks:")
            for item in self.results['passed']:
                logger.info(f"  ✅ {item}")

        if self.results['failed']:
            logger.info("\nFailed Checks:")
            for item in self.results['failed']:
                logger.info(f"  ❌ {item}")

        if self.results['warnings']:
            logger.info("\nWarnings:")
            for item in self.results['warnings']:
                logger.info(f"  ⚠️  {item}")

        # Statistics
        if self.results['stats']:
            logger.info("\nStatistics:")
            for key, value in self.results['stats'].items():
                if isinstance(value, int):
                    logger.info(f"  {key}: {value:,}")
                else:
                    logger.info(f"  {key}: {value}")

        # Overall status
        logger.info("\n" + "="*70)
        if failed == 0:
            logger.info("STATUS: ✅ MIGRATION VERIFIED SUCCESSFULLY")
            return True
        else:
            logger.info("STATUS: ❌ MIGRATION HAS ISSUES - FIX BEFORE PRODUCTION")
            return False

    async def run_verification(self):
        """Run all verification checks"""
        try:
            await self.connect()

            await self.verify_schema()
            await self.verify_data_integrity()
            await self.verify_foreign_keys()
            await self.verify_indexes()
            await self.verify_sample_data()

            success = await self.generate_report()

            await self.disconnect()

            return success

        except Exception as e:
            logger.error(f"Verification failed with error: {e}", exc_info=True)
            return False


async def main():
    """Main entry point"""
    import os
    from models.config import get_settings

    settings = get_settings()
    database_url = settings.database_url

    if not database_url:
        logger.error("DATABASE_URL not configured")
        return 1

    # Convert to async URL if needed
    if "postgresql://" in database_url:
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://")

    logger.info(f"Verifying migration for: {database_url.split('@')[1] if '@' in database_url else 'database'}")

    verifier = MigrationVerifier(database_url)
    success = await verifier.run_verification()

    return 0 if success else 1


if __name__ == '__main__':
    exit_code = asyncio.run(main())
    exit(exit_code)
