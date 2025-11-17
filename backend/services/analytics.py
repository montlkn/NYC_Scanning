import os
import posthog

posthog.project_api_key = os.getenv('POSTHOG_API_KEY')
posthog.host = 'https://app.posthog.com'

def track_scan(scan_id: str, result: dict):
    """Track scan events for analytics"""
    posthog.capture(
        distinct_id=scan_id,
        event='building_scan',
        properties={
            'confidence': result.get('confidence'),
            'num_candidates': result.get('num_candidates'),
            'processing_time_ms': result.get('processing_time_ms'),
            'has_match': result.get('status') == 'match_found',
            'bin': result.get('bin'),
        }
    )

def track_confirmation(scan_id: str, confirmed_bin: str, was_top_match: bool):
    """Track user confirmations"""
    posthog.capture(
        distinct_id=scan_id,
        event='scan_confirmed',
        properties={
            'confirmed_bin': confirmed_bin,
            'was_top_match': was_top_match,
        }
    )