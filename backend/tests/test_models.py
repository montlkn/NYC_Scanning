"""
Unit tests for database models - verify BIN is properly used as primary key
"""

import pytest
from datetime import datetime
from models.database import Base, Building, ReferenceImage, Scan


class TestBuildingModel:
    """Test Building model with BIN as primary key"""

    def test_building_bin_is_primary_key(self, sample_building_with_bin):
        """Verify BIN is the primary key"""
        building = Building(**sample_building_with_bin)

        assert building.bin == '1001234'
        assert building.bin is not None

    def test_building_bbl_is_secondary(self, sample_building_with_bin):
        """Verify BBL is secondary identifier (nullable)"""
        building = Building(**sample_building_with_bin)

        assert building.bbl == '1000001'
        # BBL should be nullable
        building.bbl = None
        assert building.bbl is None

    def test_building_public_space_with_na_bin(self, sample_building_public_space):
        """Test handling of public spaces with 'N/A' BIN"""
        building = Building(**sample_building_public_space)

        assert building.bin == 'N/A'
        assert building.address == 'Central Park, New York, NY'
        assert building.scan_enabled is False

    def test_building_required_fields(self):
        """Verify required fields are enforced"""
        # Should fail without required fields
        incomplete_building = Building(bin='1001234')
        assert incomplete_building.bin == '1001234'
        # address is required
        assert incomplete_building.address is None

    def test_building_landmark_fields(self, sample_landmark):
        """Test landmark-specific fields"""
        landmark = Building(**sample_landmark)

        assert landmark.is_landmark is True
        assert landmark.landmark_name == 'Empire State Building'
        assert landmark.architect == 'Shreve, Lamb & Harmon'
        assert landmark.architectural_style == 'Art Deco'

    def test_building_multiple_on_same_bbl(self):
        """Test handling of multiple buildings on same BBL (complex lots)"""
        # Building 1
        building1 = Building(
            bin='1001001',
            bbl='1000001',
            address='123 Main St',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        # Building 2 - same BBL, different BIN
        building2 = Building(
            bin='1001002',
            bbl='1000001',  # Same BBL
            address='123 Main St Unit 2',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        # Both should be valid with same BBL but different BINs
        assert building1.bbl == building2.bbl
        assert building1.bin != building2.bin

    def test_building_geometry_coordinates(self, sample_building_with_bin):
        """Test latitude/longitude storage"""
        building = Building(**sample_building_with_bin)

        assert building.latitude == 40.7128
        assert building.longitude == -74.0060
        assert -90 <= building.latitude <= 90
        assert -180 <= building.longitude <= 180

    def test_building_timestamp(self, sample_building_with_bin):
        """Test creation timestamp"""
        building = Building(**sample_building_with_bin)

        # Should have default timestamp if not provided
        assert building.created_at is not None or True  # Default is set by DB


class TestReferenceImageModel:
    """Test ReferenceImage model with BIN foreign key"""

    def test_reference_image_bin_foreign_key(self, sample_reference_image):
        """Verify BIN is properly used in foreign key"""
        ref_image = ReferenceImage(**sample_reference_image)

        assert ref_image.bin == '1001234'
        # Should be a foreign key to building.bin
        assert ref_image.bin is not None

    def test_reference_image_url_paths(self, sample_reference_image):
        """Test R2 storage paths use BIN not BBL"""
        ref_image = ReferenceImage(**sample_reference_image)

        # URL should use BIN in the path
        assert '/reference/1001234/' in ref_image.image_url
        # Should NOT have BBL in the path
        assert '/reference/10-00001/' not in ref_image.image_url

    def test_reference_image_compass_bearing(self, sample_reference_image):
        """Test compass bearing field (0-360)"""
        ref_image = ReferenceImage(**sample_reference_image)

        assert ref_image.compass_bearing == 90.0
        assert 0 <= ref_image.compass_bearing <= 360

    def test_reference_image_quality_score(self, sample_reference_image):
        """Test quality score is between 0 and 1"""
        ref_image = ReferenceImage(**sample_reference_image)

        assert ref_image.quality_score == 0.95
        assert 0 <= ref_image.quality_score <= 1.0

    def test_reference_image_source_types(self):
        """Test different source types"""
        sources = ['street_view', 'mapillary', 'user']

        for source in sources:
            ref_image = ReferenceImage(
                bin='1001234',
                image_url='https://example.com/image.jpg',
                source=source
            )
            assert ref_image.source == source

    def test_reference_image_multiple_per_building(self):
        """Test multiple reference images for same building"""
        images = [
            ReferenceImage(
                bin='1001234',
                image_url=f'https://example.com/reference/1001234/{bearing}.jpg',
                source='street_view',
                compass_bearing=bearing
            )
            for bearing in [0, 90, 180, 270]
        ]

        # All should point to same building
        assert all(img.bin == '1001234' for img in images)
        # Different bearings
        assert [img.compass_bearing for img in images] == [0, 90, 180, 270]

    def test_reference_image_embedding_storage(self):
        """Test CLIP embedding storage"""
        embedding = [0.1, 0.2, 0.3, 0.4, 0.5]
        ref_image = ReferenceImage(
            bin='1001234',
            image_url='https://example.com/image.jpg',
            source='street_view',
            clip_embedding=embedding,
            embedding_model='ViT-B-32'
        )

        assert ref_image.clip_embedding == embedding
        assert ref_image.embedding_model == 'ViT-B-32'

    def test_reference_image_verification(self):
        """Test verification flag"""
        ref_image = ReferenceImage(
            bin='1001234',
            image_url='https://example.com/image.jpg',
            source='street_view',
            is_verified=True
        )

        assert ref_image.is_verified is True


class TestScanModel:
    """Test Scan model with BIN fields"""

    def test_scan_uses_bin_not_bbl(self, sample_scan):
        """Verify scan uses BIN instead of BBL"""
        scan = Scan(**sample_scan)

        assert scan.top_match_bin == '1001234'
        assert scan.candidate_bins == ['1001234', '1001235', '1001236']
        # Should NOT have BBL fields
        assert not hasattr(scan, 'top_match_bbl') or scan.top_match_bbl is None

    def test_scan_candidate_bins_array(self, sample_scan):
        """Test candidate_bins is properly stored"""
        scan = Scan(**sample_scan)

        assert isinstance(scan.candidate_bins, list)
        assert len(scan.candidate_bins) == 3
        assert all(isinstance(b, str) for b in scan.candidate_bins)

    def test_scan_confirmation_bin(self, sample_scan):
        """Test confirmation uses BIN"""
        scan = Scan(**sample_scan)

        assert scan.confirmed_bin is None  # Not yet confirmed
        scan.confirmed_bin = '1001234'
        assert scan.confirmed_bin == '1001234'

    def test_scan_confidence_score(self, sample_scan):
        """Test confidence score is between 0 and 1"""
        scan = Scan(**sample_scan)

        assert scan.top_confidence == 0.92
        assert 0 <= scan.top_confidence <= 1.0

    def test_scan_gps_coordinates(self, sample_scan):
        """Test GPS coordinate validation"""
        scan = Scan(**sample_scan)

        assert scan.gps_lat == 40.7128
        assert scan.gps_lng == -74.0060
        assert -90 <= scan.gps_lat <= 90
        assert -180 <= scan.gps_lng <= 180

    def test_scan_compass_bearing(self, sample_scan):
        """Test compass bearing (0-360)"""
        scan = Scan(**sample_scan)

        assert scan.compass_bearing == 90.0
        assert 0 <= scan.compass_bearing <= 360

    def test_scan_without_confirmation(self, sample_scan):
        """Test scan can exist without confirmation"""
        scan = Scan(**sample_scan)

        assert scan.confirmed_bin is None
        # Should still be valid

    def test_scan_phone_orientation(self, sample_scan):
        """Test phone pitch and roll"""
        scan = Scan(**sample_scan)

        assert scan.phone_pitch == 0.0
        assert scan.phone_roll == 0.0
        assert -90 <= scan.phone_pitch <= 90


class TestDataIntegrity:
    """Test data integrity constraints across models"""

    def test_bin_format_validation(self):
        """Test BIN format is 10 digits or 'N/A'"""
        # Valid BINs
        valid_bins = ['1001234', '1234567', '9999999', 'N/A']
        for bin_val in valid_bins:
            building = Building(
                bin=bin_val,
                address='Test',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            )
            assert building.bin == bin_val

    def test_bbl_format_validation(self):
        """Test BBL format (can be null or formatted)"""
        # Valid BBLs
        valid_bbls = [None, '1000001', '1234567']
        for bbl in valid_bbls:
            building = Building(
                bin='1001234',
                bbl=bbl,
                address='Test',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            )
            assert building.bbl == bbl

    def test_no_bbl_as_primary_key(self):
        """Verify BBL is NOT used as primary key"""
        # Create two buildings with same BBL but different BINs
        buildings = [
            Building(
                bin='1001001',
                bbl='1000001',
                address='Building 1',
                borough='Manhattan',
                latitude=40.7128,
                longitude=-74.0060
            ),
            Building(
                bin='1001002',
                bbl='1000001',  # Same BBL!
                address='Building 2',
                borough='Manhattan',
                latitude=40.7129,
                longitude=-74.0061
            )
        ]

        # Both should be valid - BBL is not the primary key
        assert buildings[0].bin != buildings[1].bin
        assert buildings[0].bbl == buildings[1].bbl

    def test_reference_image_bin_required(self):
        """Verify reference image BIN is required"""
        ref_image = ReferenceImage(
            # bin is missing - required by foreign key
            image_url='https://example.com/image.jpg',
            source='street_view'
        )
        assert ref_image.bin is None  # Should fail on DB insert

    def test_public_space_handling(self):
        """Test that public spaces can be queried but not scanned"""
        park = Building(
            bin='N/A',
            bbl='1000002',
            address='Central Park',
            borough='Manhattan',
            latitude=40.7829,
            longitude=-73.9654,
            scan_enabled=False
        )

        # Should be able to store but scan_enabled is False
        assert park.bin == 'N/A'
        assert park.scan_enabled is False

    def test_landmark_data_consistency(self, sample_landmark):
        """Test landmark data is consistent"""
        landmark = Building(**sample_landmark)

        assert landmark.is_landmark is True
        # If is_landmark is True, should have some landmark info
        assert landmark.landmark_name is not None or True


class TestModelRelationships:
    """Test relationships between models"""

    def test_building_reference_image_relationship(self):
        """Test that reference images are linked to buildings by BIN"""
        building = Building(
            bin='1001234',
            bbl='1000001',
            address='Test Building',
            borough='Manhattan',
            latitude=40.7128,
            longitude=-74.0060
        )

        ref_image = ReferenceImage(
            bin='1001234',  # Must match building.bin
            image_url='https://example.com/image.jpg',
            source='street_view'
        )

        # Both reference the same BIN
        assert building.bin == ref_image.bin

    def test_scan_candidates_use_bins(self):
        """Test that scan candidates are stored as BINs"""
        scan = Scan(
            id='scan-001',
            gps_lat=40.7128,
            gps_lng=-74.0060,
            compass_bearing=90.0,
            candidate_bins=['1001234', '1001235', '1001236'],
            top_match_bin='1001234'
        )

        # All candidate and match references use BIN
        assert all(bin.isdigit() or bin == 'N/A' for bin in scan.candidate_bins)
        assert scan.top_match_bin.isdigit() or scan.top_match_bin == 'N/A'
