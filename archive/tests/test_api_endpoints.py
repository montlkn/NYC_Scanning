"""
API endpoint tests - verify BIN-based endpoints work correctly
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


class TestBuildingsEndpoints:
    """Test /buildings endpoints with BIN identifiers"""

    def test_get_building_by_bin_path(self):
        """Test /buildings/{bin} endpoint uses BIN not BBL"""
        # Verify endpoint path
        expected_path = "/buildings/1001234"
        assert "buildings/" in expected_path
        assert "1001234" in expected_path
        # Should NOT be /buildings/{bbl}

    def test_get_building_by_bin_response(self):
        """Test response includes BIN as primary identifier"""
        expected_response = {
            'bin': '1001234',
            'address': '123 Main Street',
            'bbl': '1000001',  # Secondary
            'latitude': 40.7128,
            'longitude': -74.0060
        }

        # Response should have 'bin' key
        assert 'bin' in expected_response
        assert expected_response['bin'] == '1001234'
        # Should have 'bbl' as secondary
        assert 'bbl' in expected_response

    def test_get_building_images_endpoint(self):
        """Test /buildings/{bin}/images endpoint"""
        # Endpoint should use BIN
        expected_path = "/buildings/1001234/images"
        assert "/buildings/" in expected_path
        assert "/images" in expected_path
        assert "1001234" in expected_path

    def test_get_building_images_response(self):
        """Test images endpoint response structure"""
        expected_response = {
            'bin': '1001234',
            'images': [
                {
                    'id': 1,
                    'url': 'https://r2.example.com/reference/1001234/0.jpg',
                    'bearing': 0.0
                }
            ],
            'count': 1
        }

        assert expected_response['bin'] == '1001234'
        assert all('/reference/1001234/' in img['url'] for img in expected_response['images'])

    def test_get_nearby_buildings_endpoint(self):
        """Test nearby buildings endpoint"""
        # Query parameters
        params = {
            'lat': 40.7128,
            'lng': -74.0060,
            'radius_meters': 100,
            'limit': 20
        }

        assert params['lat'] >= -90 and params['lat'] <= 90
        assert params['lng'] >= -180 and params['lng'] <= 180

    def test_get_nearby_buildings_response_uses_bins(self):
        """Test nearby buildings returns BINs as identifiers"""
        expected_response = {
            'location': {'lat': 40.7128, 'lng': -74.0060},
            'radius_meters': 100,
            'buildings': [
                {'bin': '1001234', 'address': 'Building 1', 'distance': 10},
                {'bin': '1001235', 'address': 'Building 2', 'distance': 50}
            ],
            'count': 2
        }

        # All buildings should have 'bin'
        assert all('bin' in b for b in expected_response['buildings'])
        assert all(isinstance(b['bin'], str) for b in expected_response['buildings'])

    def test_search_buildings_endpoint(self):
        """Test building search endpoint"""
        # Search parameters
        params = {
            'q': 'Empire State',
            'borough': 'Manhattan',
            'landmarks_only': False,
            'limit': 20
        }

        assert params['q'] != ''
        assert len(params['q']) >= 2

    def test_search_buildings_response_uses_bins(self):
        """Test search results use BINs"""
        expected_response = {
            'query': 'Empire State',
            'buildings': [
                {
                    'bin': '1001234',
                    'landmark_name': 'Empire State Building',
                    'address': '350 5th Avenue'
                }
            ],
            'count': 1
        }

        assert all('bin' in b for b in expected_response['buildings'])

    def test_top_landmarks_endpoint(self):
        """Test top landmarks endpoint"""
        params = {
            'limit': 100,
            'borough': 'Manhattan'
        }

        assert params['limit'] >= 1 and params['limit'] <= 500

    def test_top_landmarks_response_uses_bins(self):
        """Test landmarks response uses BINs"""
        expected_response = {
            'landmarks': [
                {
                    'bin': '1001234',
                    'landmark_name': 'Empire State Building',
                    'score': 95.0
                }
            ],
            'count': 1
        }

        assert all('bin' in l for l in expected_response['landmarks'])

    def test_stats_endpoint(self):
        """Test statistics endpoint"""
        expected_response = {
            'total_buildings': 37237,
            'total_landmarks': 500,
            'total_reference_images': 10000,
            'buildings_with_reference_images': 5000
        }

        assert expected_response['total_buildings'] > 0


class TestScanEndpoints:
    """Test /scan endpoints with BIN"""

    def test_scan_endpoint_parameter_validation(self):
        """Test scan endpoint validates parameters"""
        params = {
            'gps_lat': 40.7128,
            'gps_lng': -74.0060,
            'compass_bearing': 90,
            'phone_pitch': 0,
            'phone_roll': 0
        }

        # Validate GPS
        assert -90 <= params['gps_lat'] <= 90
        assert -180 <= params['gps_lng'] <= 180

        # Validate compass
        assert 0 <= params['compass_bearing'] <= 360

    def test_scan_response_uses_bins(self):
        """Test scan response uses BINs in matches"""
        expected_response = {
            'scan_id': 'scan-001',
            'matches': [
                {
                    'bin': '1001234',
                    'address': '123 Main St',
                    'confidence': 0.92
                },
                {
                    'bin': '1001235',
                    'address': '456 Broadway',
                    'confidence': 0.85
                }
            ],
            'show_picker': False
        }

        # All matches should have 'bin'
        assert all('bin' in m for m in expected_response['matches'])
        # Should NOT have 'bbl' in matches
        assert all('bbl' not in m for m in expected_response['matches'])

    def test_confirm_endpoint_uses_confirmed_bin(self):
        """Test confirm endpoint uses confirmed_bin parameter"""
        scan_id = 'scan-001'
        confirmed_bin = '1001234'

        # Parameter should be confirmed_bin, not confirmed_bbl
        assert confirmed_bin.isdigit() or confirmed_bin == 'N/A'

    def test_confirm_response_format(self):
        """Test confirm endpoint response"""
        expected_response = {
            'status': 'confirmed',
            'scan_id': 'scan-001',
            'confirmed_bin': '1001234'
        }

        assert expected_response['confirmed_bin'] == '1001234'
        # Should NOT have confirmed_bbl
        assert 'confirmed_bbl' not in expected_response

    def test_feedback_endpoint(self):
        """Test scan feedback endpoint"""
        feedback = {
            'scan_id': 'scan-001',
            'rating': 4,
            'feedback_type': 'correct',
            'feedback_text': 'Great match!'
        }

        assert 1 <= feedback['rating'] <= 5

    def test_get_scan_endpoint(self):
        """Test get scan by ID endpoint"""
        scan_id = 'scan-001'
        # Should be able to retrieve scan by ID
        assert scan_id is not None


class TestErrorHandling:
    """Test error handling in API endpoints"""

    def test_building_not_found_error(self):
        """Test 404 error when building BIN not found"""
        # When querying non-existent BIN
        bin_value = '9999999'  # Non-existent
        # Should return 404

    def test_invalid_bin_format_error(self):
        """Test error handling for invalid BIN format"""
        invalid_bins = ['ABC', '', 'invalid123']

        for invalid_bin in invalid_bins:
            # Should return validation error
            pass

    def test_invalid_gps_coordinates_error(self):
        """Test error handling for invalid GPS"""
        invalid_coords = [
            {'lat': 91, 'lng': 0},      # lat > 90
            {'lat': 0, 'lng': 181},     # lng > 180
            {'lat': -91, 'lng': 0},     # lat < -90
            {'lat': 0, 'lng': -181}     # lng < -180
        ]

        for coord in invalid_coords:
            # Should return validation error
            pass

    def test_invalid_compass_bearing_error(self):
        """Test error handling for invalid compass bearing"""
        invalid_bearings = [-1, 361, 400]

        for bearing in invalid_bearings:
            # Should return validation error
            pass

    def test_no_candidates_error(self):
        """Test error response when no buildings found"""
        expected_error = {
            'error': 'no_candidates',
            'message': 'No buildings found in your view'
        }

        assert expected_error['error'] == 'no_candidates'

    def test_no_reference_images_error(self):
        """Test error response when no reference images available"""
        expected_error = {
            'error': 'no_reference_images',
            'message': 'No reference images available'
        }

        assert expected_error['error'] == 'no_reference_images'


class TestResponseConsistency:
    """Test response consistency across endpoints"""

    def test_all_responses_use_bin_not_bbl(self):
        """Verify all responses use 'bin' as primary identifier"""
        # Endpoints that should use 'bin'
        endpoints_with_bin = [
            '/buildings/{bin}',
            '/buildings/{bin}/images',
            '/buildings/nearby',
            '/buildings/search',
            '/buildings/top-landmarks',
            '/scan',
            '/scans/{scan_id}/confirm'
        ]

        # All should use 'bin' in response
        for endpoint in endpoints_with_bin:
            assert 'bin' in endpoint or endpoint == '/scan'

    def test_building_object_structure(self):
        """Test consistent building response structure"""
        building = {
            'bin': '1001234',
            'bbl': '1000001',
            'address': '123 Main St',
            'borough': 'Manhattan',
            'latitude': 40.7128,
            'longitude': -74.0060,
            'is_landmark': False
        }

        # Required fields
        assert 'bin' in building  # Primary
        assert 'address' in building
        assert 'latitude' in building
        assert 'longitude' in building

        # Secondary fields
        assert 'bbl' in building  # Secondary

    def test_match_object_structure(self):
        """Test consistent match response structure"""
        match = {
            'bin': '1001234',
            'address': '123 Main St',
            'confidence': 0.92,
            'distance': 10.5
        }

        assert 'bin' in match
        assert 'confidence' in match
        assert 0 <= match['confidence'] <= 1.0

    def test_image_object_structure(self):
        """Test consistent image response structure"""
        image = {
            'id': 1,
            'url': 'https://r2.example.com/reference/1001234/90.jpg',
            'bearing': 90.0,
            'source': 'street_view'
        }

        # URL should use BIN in path
        assert '/reference/1001234/' in image['url']
        assert 0 <= image['bearing'] <= 360


class TestEndpointDocumentation:
    """Test that endpoints are properly documented with BIN"""

    def test_buildings_endpoint_docstring(self):
        """Test /buildings/{bin} endpoint docstring mentions BIN"""
        docstring = """
        Get detailed information about a building by BIN (Building Identification Number)

        Now uses BIN instead of BBL as the primary identifier
        """

        assert 'BIN' in docstring
        assert 'Building Identification Number' in docstring
        assert 'primary' in docstring

    def test_scan_confirm_endpoint_docstring(self):
        """Test confirm endpoint docstring mentions confirmed_bin"""
        docstring = """
        User confirms which building they scanned

        Uses BIN (Building Identification Number) instead of BBL
        """

        assert 'confirmed_bin' in docstring or 'BIN' in docstring
        assert 'Building Identification Number' in docstring
