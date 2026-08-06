import os
import posthog

_posthog_key = os.getenv('POSTHOG_API_KEY')
posthog.project_api_key = _posthog_key
posthog.host = 'https://app.posthog.com'
_enabled = bool(_posthog_key)


def track_confirmation(scan_id: str, confirmed_bin: str, was_top_match: bool):
    if not _enabled:
        return
    posthog.capture(
        distinct_id=scan_id,
        event='scan_confirmed',
        properties={
            'confirmed_bin': confirmed_bin,
            'was_top_match': was_top_match,
        }
    )