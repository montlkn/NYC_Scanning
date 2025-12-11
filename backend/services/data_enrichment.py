"""
Data enrichment service - uses Exa AI and web search to find building information
Automatically enriches user-contributed buildings with architect, year, style, etc.
"""

import logging
import httpx
import json
from typing import Dict, Optional
from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def enrich_building_with_exa(
    address: str,
    building_name: Optional[str] = None,
    existing_data: Optional[Dict] = None
) -> Dict:
    """
    Use Exa AI to search for building information and enrich metadata.

    Args:
        address: Building address
        building_name: Optional building name
        existing_data: Any existing metadata we already have

    Returns:
        dict with enriched data:
        {
            'architect': str,
            'year_built': int,
            'architectural_style': str,
            'building_use': str,
            'notable_features': str,
            'landmark_status': str,
            'sources': [list of URLs],
            'confidence': float (0-1),
            'raw_results': [...]
        }
    """
    try:
        if not hasattr(settings, 'exa_api_key') or not settings.exa_api_key:
            logger.warning("Exa API key not configured, skipping enrichment")
            return {'error': 'Exa API key not configured'}

        # Build search query
        search_terms = []
        if building_name:
            search_terms.append(building_name)
        search_terms.append(address)
        search_terms.extend(['architect', 'architecture', 'history', 'built'])

        query = " ".join(search_terms)

        # Call Exa search API
        url = "https://api.exa.ai/search"
        headers = {
            'Content-Type': 'application/json',
            'x-api-key': settings.exa_api_key
        }
        payload = {
            'query': query,
            'type': 'neural',  # Use AI-powered search
            'num_results': 5,
            'contents': {
                'text': True,
                'highlights': True
            },
            'category': 'company'  # Architecture/real estate category
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        # Extract enrichment data from results
        enriched = _extract_building_info_from_exa_results(
            data,
            address,
            building_name,
            existing_data
        )

        logger.info(f"Enriched building {address} with confidence={enriched.get('confidence', 0):.2f}")
        return enriched

    except Exception as e:
        logger.error(f"Error enriching building with Exa: {e}", exc_info=True)
        return {'error': str(e), 'confidence': 0}


def _extract_building_info_from_exa_results(
    exa_response: Dict,
    address: str,
    building_name: Optional[str],
    existing_data: Optional[Dict]
) -> Dict:
    """
    Parse Exa search results and extract structured building information.

    Uses heuristics and keyword matching to identify:
    - Architect name
    - Year built
    - Architectural style
    - Notable features
    - Landmark status
    """
    enriched = {
        'architect': None,
        'year_built': None,
        'architectural_style': None,
        'building_use': None,
        'notable_features': None,
        'landmark_status': None,
        'sources': [],
        'raw_results': [],
        'confidence': 0.0
    }

    if 'results' not in exa_response:
        return enriched

    results = exa_response['results']
    enriched['raw_results'] = results

    # Collect all text snippets
    all_text = []
    for result in results:
        enriched['sources'].append(result.get('url', ''))
        if 'text' in result:
            all_text.append(result['text'])
        if 'highlights' in result:
            all_text.extend(result['highlights'])

    combined_text = " ".join(all_text).lower()

    # Extract architect
    architect_keywords = ['designed by', 'architect', 'designed for', 'by architect']
    for keyword in architect_keywords:
        if keyword in combined_text:
            # Simple extraction: get text after keyword
            idx = combined_text.find(keyword)
            snippet = combined_text[idx:idx+100]
            # Extract name (heuristic: capitalized words after keyword)
            parts = snippet.split()
            if len(parts) > 2:
                potential_name = " ".join(parts[2:5])  # Get next 3 words
                if potential_name and len(potential_name) > 3:
                    enriched['architect'] = potential_name.title()
                    break

    # Extract year built
    import re
    year_pattern = r'\b(18|19|20)\d{2}\b'
    years = re.findall(year_pattern, combined_text)
    if years:
        # Take the most common year or earliest plausible year
        year_counts = {}
        for year_str in years:
            year = int(year_str)
            if 1800 <= year <= 2024:  # Sanity check
                year_counts[year] = year_counts.get(year, 0) + 1

        if year_counts:
            enriched['year_built'] = max(year_counts, key=year_counts.get)

    # Extract architectural style
    style_keywords = {
        'art deco': 'Art Deco',
        'gothic': 'Gothic',
        'neogothic': 'Neo-Gothic',
        'neo-gothic': 'Neo-Gothic',
        'beaux-arts': 'Beaux-Arts',
        'beaux arts': 'Beaux-Arts',
        'romanesque': 'Romanesque',
        'modern': 'Modern',
        'postmodern': 'Postmodern',
        'post-modern': 'Postmodern',
        'contemporary': 'Contemporary',
        'classical': 'Classical',
        'neoclassical': 'Neoclassical',
        'neo-classical': 'Neoclassical',
        'victorian': 'Victorian',
        'colonial': 'Colonial',
        'federal': 'Federal',
        'greek revival': 'Greek Revival',
        'italianate': 'Italianate',
        'second empire': 'Second Empire',
        'queen anne': 'Queen Anne',
        'chicago school': 'Chicago School',
        'international style': 'International Style',
        'brutalist': 'Brutalist',
        'bauhaus': 'Bauhaus'
    }

    for keyword, style_name in style_keywords.items():
        if keyword in combined_text:
            enriched['architectural_style'] = style_name
            break

    # Extract landmark status
    if 'landmark' in combined_text or 'historic' in combined_text:
        if 'national register' in combined_text:
            enriched['landmark_status'] = 'national_register'
        elif 'designated landmark' in combined_text or 'nyc landmark' in combined_text:
            enriched['landmark_status'] = 'designated'
        else:
            enriched['landmark_status'] = 'historic_district'

    # Extract notable features
    feature_keywords = ['facade', 'cornice', 'ornament', 'detail', 'terracotta', 'limestone', 'brick', 'stone']
    notable_parts = []
    for keyword in feature_keywords:
        if keyword in combined_text:
            notable_parts.append(keyword)
    if notable_parts:
        enriched['notable_features'] = ", ".join(notable_parts[:3])

    # Calculate confidence score
    confidence_score = 0
    if enriched['architect']:
        confidence_score += 0.3
    if enriched['year_built']:
        confidence_score += 0.25
    if enriched['architectural_style']:
        confidence_score += 0.25
    if enriched['landmark_status']:
        confidence_score += 0.1
    if enriched['notable_features']:
        confidence_score += 0.1

    enriched['confidence'] = min(confidence_score, 1.0)

    return enriched


async def enrich_building_with_fallback(
    address: str,
    building_name: Optional[str] = None,
    existing_data: Optional[Dict] = None
) -> Dict:
    """
    Try multiple enrichment sources in order of preference:
    1. Exa AI
    2. (Future: Wikipedia, Google Places, etc.)

    Returns enriched data with highest confidence
    """
    # Try Exa first
    exa_result = await enrich_building_with_exa(address, building_name, existing_data)

    if exa_result.get('confidence', 0) > 0.5:
        logger.info(f"Exa enrichment successful for {address}")
        return {**exa_result, 'enrichment_source': 'exa'}

    # Future: Add fallback to other sources here
    # - Wikipedia API
    # - Google Places API
    # - NYC Landmarks Preservation Commission database

    logger.info(f"Enrichment completed for {address} with confidence={exa_result.get('confidence', 0)}")
    return {**exa_result, 'enrichment_source': 'exa'}
