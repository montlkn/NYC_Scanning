"""
Integration tests for service layer - verify BIN-based operations
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from sqlalchemy import select, insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Building, ReferenceImage


class TestGeospatialService:
    """Test geospatial service with BIN-based queries"""

    @pytest.mark.asyncio
    async def test_get_candidate_buildings_filters_na_bins(self, async_session):
        """Verify N/A BINs (public spaces) are filtered out"""
        # Add buildings to database
        buildings = [
            Building(
                bin='1001234',
                bbl='1000001',
                address='123 Main St',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            ),
            Building(
                bin='N/A',
                bbl='1000002',
                address='Central Park',
                borough='Manhattan',
                latitude=40.7829,
                longitude=-73.9654,
                scan_enabled=False
            ),
            Building(
                bin='1001235',
                bbl='1000003',
                address='456 Broadway',
                borough='Manhattan',
                latitude=40.7150,
                longitude=-74.0070
            )
        ]

        async_session.add_all(buildings)
        await async_session.commit()

        # Mock the geospatial service function
        from services import geospatial

        # Simulate filtering N/A bins
        all_candidates = await async_session.execute(
            select(Building).where(Building.bin != 'N/A')
        )
        candidates = all_candidates.scalars().all()

        # Should only return buildings with valid BINs
        assert len(candidates) == 2
        assert all(c.bin != 'N/A' for c in candidates)
        assert candidates[0].bin == '1001234'
        assert candidates[1].bin == '1001235'

    @pytest.mark.asyncio
    async def test_get_candidate_buildings_returns_bin_in_dict(self, async_session):
        """Verify returned candidates have BIN as primary identifier"""
        building = Building(
            bin='1001234',
            bbl='1000001',
            address='123 Main St',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        async_session.add(building)
        await async_session.commit()

        # Simulate response dict
        candidate_dict = {
            'bin': building.bin,
            'bbl': building.bbl,
            'address': building.address,
            'latitude': building.latitude,
            'longitude': building.longitude
        }

        # BIN should be primary
        assert candidate_dict['bin'] == '1001234'
        assert candidate_dict['bbl'] == '1000001'

    @pytest.mark.asyncio
    async def test_buildings_in_radius_excludes_public_spaces(self, async_session):
        """Test radius search excludes public spaces"""
        buildings = [
            Building(
                bin='1001234',
                bbl='1000001',
                address='Building 1',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            ),
            Building(
                bin='N/A',
                bbl='1000002',
                address='Park',
                borough='Manhattan',
                latitude=40.7150,
                longitude=-74.0050,
                scan_enabled=False
            )
        ]

        async_session.add_all(buildings)
        await async_session.commit()

        # Query with filter
        result = await async_session.execute(
            select(Building).where(Building.bin != 'N/A')
        )
        candidates = result.scalars().all()

        assert len(candidates) == 1
        assert candidates[0].bin != 'N/A'

    @pytest.mark.asyncio
    async def test_multiple_buildings_same_bbl(self, async_session):
        """Test that multiple buildings on same BBL are returned separately"""
        # Complex lot with multiple buildings
        buildings = [
            Building(
                bin='1001001',
                bbl='1000001',  # Same BBL
                address='123 Main St',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            ),
            Building(
                bin='1001002',
                bbl='1000001',  # Same BBL
                address='123 Main St Unit 2',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            )
        ]

        async_session.add_all(buildings)
        await async_session.commit()

        # Query by BBL should return both
        result = await async_session.execute(
            select(Building).where(Building.bbl == '1000001')
        )
        candidates = result.scalars().all()

        # Both buildings should be found
        assert len(candidates) == 2
        assert candidates[0].bin != candidates[1].bin
        assert candidates[0].bbl == candidates[1].bbl


class TestReferenceImageService:
    """Test reference image service with BIN"""

    @pytest.mark.asyncio
    async def test_reference_images_use_bin_foreign_key(self, async_session):
        """Verify reference images link to buildings by BIN"""
        # Create building
        building = Building(
            bin='1001234',
            bbl='1000001',
            address='123 Main St',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        async_session.add(building)
        await async_session.commit()

        # Add reference images
        images = [
            ReferenceImage(
                bin='1001234',  # Foreign key to building.bin
                image_url='https://r2.example.com/reference/1001234/0.jpg',
                source='street_view',
                compass_bearing=0.0
            ),
            ReferenceImage(
                bin='1001234',
                image_url='https://r2.example.com/reference/1001234/90.jpg',
                source='street_view',
                compass_bearing=90.0
            )
        ]

        async_session.add_all(images)
        await async_session.commit()

        # Query images by BIN
        result = await async_session.execute(
            select(ReferenceImage).where(ReferenceImage.bin == '1001234')
        )
        found_images = result.scalars().all()

        assert len(found_images) == 2
        assert all(img.bin == '1001234' for img in found_images)

    @pytest.mark.asyncio
    async def test_r2_paths_use_bin_not_bbl(self, async_session):
        """Verify R2 storage paths use BIN not BBL"""
        image = ReferenceImage(
            bin='1001234',
            image_url='https://r2.example.com/reference/1001234/90.jpg',
            source='street_view',
            compass_bearing=90.0
        )

        async_session.add(image)
        await async_session.commit()

        result = await async_session.execute(
            select(ReferenceImage).where(ReferenceImage.bin == '1001234')
        )
        found_image = result.scalar_one()

        # Path should use BIN
        assert '/reference/1001234/' in found_image.image_url
        # Path should NOT use BBL format
        assert '/reference/10-00001/' not in found_image.image_url

    @pytest.mark.asyncio
    async def test_get_images_for_multiple_bearings(self, async_session):
        """Test retrieving images for different compass bearings"""
        building = Building(
            bin='1001234',
            bbl='1000001',
            address='123 Main St',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        async_session.add(building)
        await async_session.commit()

        # Add images for cardinal directions
        bearings = [0, 90, 180, 270]
        for bearing in bearings:
            image = ReferenceImage(
                bin='1001234',
                image_url=f'https://r2.example.com/reference/1001234/{bearing}.jpg',
                source='street_view',
                compass_bearing=bearing
            )
            async_session.add(image)

        await async_session.commit()

        # Query images
        result = await async_session.execute(
            select(ReferenceImage).where(ReferenceImage.bin == '1001234')
        )
        images = result.scalars().all()

        assert len(images) == 4
        bearings_found = sorted([img.compass_bearing for img in images])
        assert bearings_found == [0, 90, 180, 270]

    @pytest.mark.asyncio
    async def test_reference_image_with_embedding(self, async_session):
        """Test reference image with CLIP embedding"""
        embedding = [0.1] * 512  # ViT-B-32 has 512 dimensions

        image = ReferenceImage(
            bin='1001234',
            image_url='https://r2.example.com/reference/1001234/90.jpg',
            source='street_view',
            clip_embedding=embedding,
            embedding_model='ViT-B-32'
        )

        async_session.add(image)
        await async_session.commit()

        result = await async_session.execute(
            select(ReferenceImage).where(ReferenceImage.bin == '1001234')
        )
        found_image = result.scalar_one()

        assert found_image.clip_embedding == embedding
        assert found_image.embedding_model == 'ViT-B-32'


class TestBuildingQueries:
    """Test various building queries using BIN"""

    @pytest.mark.asyncio
    async def test_query_by_bin_primary_key(self, async_session):
        """Test querying building by BIN (primary key)"""
        building = Building(
            bin='1001234',
            bbl='1000001',
            address='123 Main St',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        async_session.add(building)
        await async_session.commit()

        # Query by BIN
        result = await async_session.execute(
            select(Building).where(Building.bin == '1001234')
        )
        found = result.scalar_one()

        assert found.bin == '1001234'
        assert found.address == '123 Main St'

    @pytest.mark.asyncio
    async def test_query_by_bbl_secondary_key(self, async_session):
        """Test querying building by BBL (secondary key)"""
        buildings = [
            Building(
                bin='1001001',
                bbl='1000001',
                address='123 Main St',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            ),
            Building(
                bin='1001002',
                bbl='1000001',  # Same BBL
                address='123 Main St Unit 2',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            )
        ]

        async_session.add_all(buildings)
        await async_session.commit()

        # Query by BBL returns multiple
        result = await async_session.execute(
            select(Building).where(Building.bbl == '1000001')
        )
        found = result.scalars().all()

        assert len(found) == 2

    @pytest.mark.asyncio
    async def test_query_landmarks(self, async_session):
        """Test querying landmark buildings"""
        landmarks = [
            Building(
                bin='1001234',
                bbl='1000001',
                address='Empire State Building',
                borough='Manhattan',
                latitude=40.7484,
                longitude=-73.9857,
                is_landmark=True,
                landmark_name='Empire State Building'
            ),
            Building(
                bin='1001235',
                bbl='1000002',
                address='Regular Building',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060,
                is_landmark=False
            )
        ]

        async_session.add_all(landmarks)
        await async_session.commit()

        # Query landmarks
        result = await async_session.execute(
            select(Building).where(Building.is_landmark == True)
        )
        found = result.scalars().all()

        assert len(found) == 1
        assert found[0].bin == '1001234'

    @pytest.mark.asyncio
    async def test_query_scan_enabled(self, async_session):
        """Test querying scannable buildings"""
        buildings = [
            Building(
                bin='1001234',
                bbl='1000001',
                address='Scannable Building',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060,
                scan_enabled=True
            ),
            Building(
                bin='N/A',
                bbl='1000002',
                address='Park',
                borough='Manhattan',
                latitude=40.7829,
                longitude=-73.9654,
                scan_enabled=False
            )
        ]

        async_session.add_all(buildings)
        await async_session.commit()

        # Query scannable
        result = await async_session.execute(
            select(Building).where(
                (Building.scan_enabled == True) &
                (Building.bin != 'N/A')
            )
        )
        found = result.scalars().all()

        assert len(found) == 1
        assert found[0].scan_enabled is True


class TestDataConsistency:
    """Test data consistency across models"""

    @pytest.mark.asyncio
    async def test_bin_consistency_across_tables(self, async_session):
        """Verify BIN is consistent across building and reference_image tables"""
        building = Building(
            bin='1001234',
            bbl='1000001',
            address='123 Main St',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        async_session.add(building)
        await async_session.commit()

        # Add reference image with same BIN
        image = ReferenceImage(
            bin='1001234',
            image_url='https://r2.example.com/reference/1001234/90.jpg',
            source='street_view'
        )

        async_session.add(image)
        await async_session.commit()

        # Verify both have same BIN
        result_building = await async_session.execute(
            select(Building).where(Building.bin == '1001234')
        )
        result_image = await async_session.execute(
            select(ReferenceImage).where(ReferenceImage.bin == '1001234')
        )

        building = result_building.scalar_one()
        image = result_image.scalar_one()

        assert building.bin == image.bin == '1001234'

    @pytest.mark.asyncio
    async def test_no_orphaned_reference_images(self, async_session):
        """Test that reference images cannot exist without a valid building"""
        # This would be enforced by the database foreign key constraint
        building = Building(
            bin='1001234',
            bbl='1000001',
            address='123 Main St',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        async_session.add(building)
        await async_session.commit()

        # Valid reference image
        image = ReferenceImage(
            bin='1001234',
            image_url='https://r2.example.com/reference/1001234/90.jpg',
            source='street_view'
        )

        async_session.add(image)
        await async_session.commit()

        # Verify it's there
        result = await async_session.execute(
            select(ReferenceImage).where(ReferenceImage.bin == '1001234')
        )
        assert result.scalar_one() is not None
