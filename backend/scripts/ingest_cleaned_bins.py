#!/usr/bin/env python3
"""
Ingest cleaned BIN data into the database

This script loads the full_dataset_fixed_bins.csv (cleaned and validated data)
into the buildings_full_merge_scanning table, replacing the old BBL-based data
with proper BIN-based primary keys.

CRITICAL STEP: This is the actual migration of data into the database.
"""

import asyncio
import logging
import pandas as pd
from pathlib import Path
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import insert, delete, text
from models.database import Building
from models.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BINDataIngester:
    """Load cleaned BIN data into database"""

    def __init__(self, database_url: str, csv_path: str):
        self.database_url = database_url
        self.csv_path = csv_path
        self.engine = None
        self.stats = {
            'total_rows': 0,
            'inserted': 0,
            'updated': 0,
            'errors': 0,
            'public_spaces': 0,
            'duplicates': 0
        }

    async def connect(self):
        """Create database connection"""
        self.engine = create_async_engine(self.database_url, echo=False)
        logger.info("Connected to database")

    async def disconnect(self):
        """Close database connection"""
        if self.engine:
            await self.engine.dispose()

    async def backup_existing_data(self):
        """Backup existing building data before replacing"""
        async with AsyncSession(self.engine) as session:
            try:
                # Create backup table if it doesn't exist
                await session.execute(
                    text("""
                        CREATE TABLE IF NOT EXISTS buildings_backup_pre_bin_migration AS
                        SELECT * FROM buildings_full_merge_scanning
                        WHERE NOT EXISTS (
                            SELECT 1 FROM buildings_backup_pre_bin_migration
                        )
                    """)
                )
                await session.commit()
                logger.info("✅ Backup created: buildings_backup_pre_bin_migration")
            except Exception as e:
                logger.warning(f"⚠️  Could not backup existing data: {e}")

    async def clear_existing_data(self):
        """Clear existing data (keeping backup)"""
        async with AsyncSession(self.engine) as session:
            try:
                await session.execute(delete(Building))
                await session.commit()
                logger.info("✅ Cleared existing building data")
            except Exception as e:
                logger.error(f"❌ Failed to clear data: {e}")
                raise

    def load_csv_data(self) -> pd.DataFrame:
        """Load CSV file"""
        logger.info(f"Loading CSV: {self.csv_path}")
        df = pd.read_csv(self.csv_path)
        self.stats['total_rows'] = len(df)
        logger.info(f"Loaded {len(df):,} rows from CSV")
        return df

    def prepare_buildings_for_insert(self, df: pd.DataFrame) -> list:
        """Convert CSV rows to Building model instances"""
        buildings = []
        public_space_count = 0
        duplicate_count = 0

        for idx, row in df.iterrows():
            try:
                # Parse BIN
                bin_val = str(row.get('bin', '')).strip()
                if bin_val == 'N/A':
                    public_space_count += 1

                # Parse BBL
                bbl_val = str(row.get('bbl', '')).strip()
                if pd.isna(row.get('bbl')) or bbl_val == 'nan' or bbl_val == '':
                    bbl_val = None

                # Create building record
                building_data = {
                    'bin': bin_val,
                    'bbl': bbl_val,
                    'address': str(row.get('address', '')).strip() or None,
                    'borough': str(row.get('borough', '')).strip() or None,
                    'latitude': float(row['latitude']) if pd.notna(row.get('latitude')) else None,
                    'longitude': float(row['longitude']) if pd.notna(row.get('longitude')) else None,
                    'year_built': int(row['year_built']) if pd.notna(row.get('year_built')) else None,
                    'num_floors': int(row['num_floors']) if pd.notna(row.get('num_floors')) else None,
                    'building_class': str(row.get('building_class', '')).strip() or None,
                    'land_use': str(row.get('land_use', '')).strip() or None,
                    'is_landmark': bool(row.get('is_landmark', False)),
                    'landmark_name': str(row.get('landmark_name', '')).strip() or None,
                    'architect': str(row.get('architect', '')).strip() or None,
                    'architectural_style': str(row.get('architectural_style', '')).strip() or None,
                    'short_bio': str(row.get('short_bio', '')).strip() or None,
                    'scan_enabled': bin_val != 'N/A',  # Don't scan public spaces
                }

                building = Building(**building_data)
                buildings.append(building)

            except Exception as e:
                logger.warning(f"Error processing row {idx}: {e}")
                self.stats['errors'] += 1
                continue

        self.stats['public_spaces'] = public_space_count
        logger.info(f"Prepared {len(buildings):,} buildings for insertion")
        logger.info(f"  - Public spaces (N/A BIN): {public_space_count}")

        return buildings

    async def insert_buildings_batch(self, buildings: list, batch_size: int = 1000):
        """Insert buildings in batches"""
        async with AsyncSession(self.engine) as session:
            for i in range(0, len(buildings), batch_size):
                batch = buildings[i:i + batch_size]
                try:
                    session.add_all(batch)
                    await session.commit()
                    self.stats['inserted'] += len(batch)

                    if (i + batch_size) % 5000 == 0:
                        logger.info(f"  Inserted {i + batch_size:,} / {len(buildings):,}")

                except Exception as e:
                    logger.error(f"Error inserting batch {i}: {e}")
                    self.stats['errors'] += batch_size
                    await session.rollback()

        logger.info(f"✅ Inserted {self.stats['inserted']:,} buildings successfully")

    async def verify_insertion(self):
        """Verify data was inserted correctly"""
        async with AsyncSession(self.engine) as session:
            # Count total
            result = await session.execute(text("SELECT COUNT(*) FROM buildings_full_merge_scanning"))
            total = result.scalar()
            logger.info(f"Total buildings in database: {total:,}")

            # Count by BIN validity
            result = await session.execute(text("""
                SELECT
                    COUNT(*) as total,
                    COUNT(CASE WHEN bin != 'N/A' THEN 1 END) as with_valid_bin,
                    COUNT(CASE WHEN bin = 'N/A' THEN 1 END) as public_spaces
                FROM buildings_full_merge_scanning
            """))

            row = result.fetchone()
            total, with_valid, public = row

            logger.info(f"BIN Distribution:")
            logger.info(f"  - Valid BINs: {with_valid:,} ({with_valid/total*100:.2f}%)")
            logger.info(f"  - Public spaces (N/A): {public:,}")

            # Sample verification
            result = await session.execute(text("""
                SELECT bin, bbl, address, borough
                FROM buildings_full_merge_scanning
                WHERE bin != 'N/A'
                LIMIT 5
            """))

            logger.info("\nSample buildings with valid BINs:")
            for building in result.fetchall():
                bin_val, bbl_val, addr, boro = building
                logger.info(f"  BIN: {bin_val}, BBL: {bbl_val}, Address: {addr[:40]}")

            # Sample public spaces
            result = await session.execute(text("""
                SELECT bin, address, borough
                FROM buildings_full_merge_scanning
                WHERE bin = 'N/A'
                LIMIT 3
            """))

            logger.info("\nSample public spaces (N/A):")
            for building in result.fetchall():
                bin_val, addr, boro = building
                logger.info(f"  N/A: {addr[:40]}")

    async def generate_report(self):
        """Generate ingestion report"""
        logger.info("\n" + "="*70)
        logger.info("BIN DATA INGESTION REPORT")
        logger.info("="*70)
        logger.info(f"CSV File: {self.csv_path}")
        logger.info(f"Ingestion Date: {datetime.now().isoformat()}")
        logger.info("")
        logger.info(f"Total Rows in CSV: {self.stats['total_rows']:,}")
        logger.info(f"Successfully Inserted: {self.stats['inserted']:,}")
        logger.info(f"Errors: {self.stats['errors']}")
        logger.info(f"Public Spaces (N/A): {self.stats['public_spaces']}")
        logger.info(f"Success Rate: {self.stats['inserted']/self.stats['total_rows']*100:.2f}%")
        logger.info("")
        logger.info("="*70)

        if self.stats['errors'] == 0:
            logger.info("✅ INGESTION SUCCESSFUL - DATA READY FOR TESTING")
        else:
            logger.warning(f"⚠️  {self.stats['errors']} errors during ingestion")

    async def run_ingestion(self):
        """Run complete ingestion process"""
        try:
            await self.connect()

            # 1. Backup existing data
            logger.info("\n[1/5] Backing up existing data...")
            await self.backup_existing_data()

            # 2. Clear old data
            logger.info("\n[2/5] Clearing existing building data...")
            await self.clear_existing_data()

            # 3. Load and prepare data
            logger.info("\n[3/5] Loading and preparing CSV data...")
            df = self.load_csv_data()
            buildings = self.prepare_buildings_for_insert(df)

            # 4. Insert data
            logger.info("\n[4/5] Inserting buildings into database...")
            await self.insert_buildings_batch(buildings)

            # 5. Verify
            logger.info("\n[5/5] Verifying ingestion...")
            await self.verify_insertion()

            # Generate report
            await self.generate_report()

            await self.disconnect()

            return True

        except Exception as e:
            logger.error(f"Ingestion failed: {e}", exc_info=True)
            await self.disconnect()
            return False


async def main():
    """Main entry point"""
    settings = get_settings()

    # Use async PostgreSQL URL
    database_url = settings.database_url
    if "postgresql://" in database_url:
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://")

    # Path to cleaned CSV
    csv_path = Path(__file__).parent.parent / "data" / "final" / "full_dataset_fixed_bins.csv"

    if not csv_path.exists():
        logger.error(f"CSV file not found: {csv_path}")
        return 1

    logger.info(f"Starting BIN data ingestion")
    logger.info(f"Database: {database_url.split('@')[1] if '@' in database_url else 'unknown'}")
    logger.info(f"Data file: {csv_path}")

    ingester = BINDataIngester(database_url, str(csv_path))
    success = await ingester.run_ingestion()

    return 0 if success else 1


if __name__ == '__main__':
    exit_code = asyncio.run(main())
    exit(exit_code)
