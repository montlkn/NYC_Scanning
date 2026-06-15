"""
Building contribution service - handles crowdsourced building data
Includes address geocoding, BIN/BBL lookup from PLUTO/BUILDING datasets
"""

import logging
import pandas as pd
import httpx
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from math import radians, cos, sin, asin, sqrt

from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Load PLUTO and BUILDING data (cached in memory)
_pluto_df = None
_building_df = None

DATA_DIR = Path(__file__).parent.parent / "data"


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance in meters between two points
    on the earth (specified in decimal degrees)
    """
    # convert decimal degrees to radians
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])

    # haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    r = 6371000  # Radius of earth in meters
    return c * r


def load_pluto_data() -> pd.DataFrame:
    """Load PLUTO dataset (cached)"""
    global _pluto_df
    if _pluto_df is None:
        pluto_path = DATA_DIR / "pluto_for_supabase.csv"
        logger.info(f"Loading PLUTO data from {pluto_path}")
        _pluto_df = pd.read_csv(pluto_path)
        logger.info(f"Loaded {len(_pluto_df)} PLUTO records")
    return _pluto_df


def load_building_data() -> pd.DataFrame:
    """Load BUILDING dataset (cached)"""
    global _building_df
    if _building_df is None:
        building_path = DATA_DIR / "BUILDING_20251104.csv"
        logger.info(f"Loading BUILDING data from {building_path}")
        # Only load columns we need to save memory
        _building_df = pd.read_csv(
            building_path,
            usecols=['BIN', 'BASE_BBL', 'Construction Year', 'Height Roof']
        )
        logger.info(f"Loaded {len(_building_df)} BUILDING records")
    return _building_df


async def reverse_geocode_google(lat: float, lng: float) -> List[Dict[str, str]]:
    """
    Reverse geocode GPS coordinates to multiple address options using Google Maps API.

    Returns list of address options sorted by relevance:
    [
        {
            'address': '123 Main St, New York, NY 10001',
            'formatted_address': '123 Main St',
            'street_number': '123',
            'street_name': 'Main St',
            'zip_code': '10001',
            'place_id': 'ChIJ...'
        },
        ...
    ]
    """
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            'latlng': f"{lat},{lng}",
            'key': settings.google_maps_api_key,
            'result_type': 'street_address|premise'  # Only building-level addresses
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        if data['status'] != 'OK':
            logger.warning(f"Google Geocoding API returned status: {data['status']}")
            return []

        address_options = []
        for result in data.get('results', [])[:5]:  # Top 5 results
            address_components = {}
            for component in result.get('address_components', []):
                if 'street_number' in component['types']:
                    address_components['street_number'] = component['long_name']
                elif 'route' in component['types']:
                    address_components['street_name'] = component['long_name']
                elif 'postal_code' in component['types']:
                    address_components['zip_code'] = component['long_name']

            address_options.append({
                'address': result['formatted_address'],
                'formatted_address': result['formatted_address'].split(',')[0],  # Just street
                'street_number': address_components.get('street_number', ''),
                'street_name': address_components.get('street_name', ''),
                'zip_code': address_components.get('zip_code', ''),
                'place_id': result.get('place_id', ''),
                'lat': result['geometry']['location']['lat'],
                'lng': result['geometry']['location']['lng']
            })

        logger.info(f"Found {len(address_options)} address options for ({lat}, {lng})")
        return address_options

    except Exception as e:
        logger.error(f"Error reverse geocoding ({lat}, {lng}): {e}", exc_info=True)
        return []


def lookup_bin_from_gps(lat: float, lng: float, radius_meters: float = 50) -> Optional[Tuple[str, str]]:
    """
    Look up BIN and BBL from GPS coordinates using PLUTO dataset.

    Returns (BIN, BBL) tuple or None if not found.
    Searches within radius_meters of the GPS point.
    """
    try:
        pluto_df = load_pluto_data()

        # Filter to buildings within radius
        pluto_df['distance'] = pluto_df.apply(
            lambda row: haversine_distance(lat, lng, row['latitude'], row['longitude']),
            axis=1
        )

        nearby = pluto_df[pluto_df['distance'] <= radius_meters].sort_values('distance')

        if len(nearby) == 0:
            logger.warning(f"No buildings found within {radius_meters}m of ({lat}, {lng})")
            return None

        # Get closest building
        closest = nearby.iloc[0]
        bbl = str(closest['bbl'])

        # Now look up BIN from BUILDING dataset using BBL
        building_df = load_building_data()
        bin_match = building_df[building_df['BASE_BBL'] == bbl]

        if len(bin_match) > 0:
            bin_value = str(bin_match.iloc[0]['BIN'])
            logger.info(f"Found BIN={bin_value}, BBL={bbl} at distance={closest['distance']:.1f}m")
            return (bin_value, bbl)
        else:
            logger.warning(f"Found BBL={bbl} but no matching BIN in BUILDING dataset")
            return (None, bbl)  # Return BBL even if no BIN

    except Exception as e:
        logger.error(f"Error looking up BIN from GPS ({lat}, {lng}): {e}", exc_info=True)
        return None


def get_building_metadata_from_pluto(bbl: str) -> Optional[Dict]:
    """
    Get building metadata from PLUTO dataset by BBL.

    Returns dict with: year_built, num_floors, building_class, lot_area, etc.
    """
    try:
        pluto_df = load_pluto_data()
        match = pluto_df[pluto_df['bbl'] == bbl]

        if len(match) == 0:
            return None

        row = match.iloc[0]
        return {
            'year_built': int(row['year_built']) if pd.notna(row['year_built']) else None,
            'num_floors': int(row['num_floors']) if pd.notna(row['num_floors']) else None,
            'building_class': row['building_class'] if pd.notna(row['building_class']) else None,
            'lot_area': float(row['lot_area']) if pd.notna(row['lot_area']) else None,
            'building_area': float(row['building_area']) if pd.notna(row['building_area']) else None,
            'land_use': row['land_use'] if pd.notna(row['land_use']) else None,
            'is_landmark': bool(row['is_landmark']) if pd.notna(row['is_landmark']) else False,
        }
    except Exception as e:
        logger.error(f"Error getting PLUTO metadata for BBL {bbl}: {e}")
        return None


def get_building_height_from_building_dataset(bin_value: str) -> Optional[float]:
    """Get building height from BUILDING dataset by BIN"""
    try:
        building_df = load_building_data()
        match = building_df[building_df['BIN'] == bin_value]

        if len(match) > 0:
            height = match.iloc[0]['Height Roof']
            return float(height) if pd.notna(height) else None
        return None
    except Exception as e:
        logger.error(f"Error getting height for BIN {bin_value}: {e}")
        return None
