"""
Structured per-scan telemetry.

Logs to:
 1. The Python logger (always, at DEBUG level — visible in Modal logs)
 2. The analytics service (PostHog, if configured)

Key metrics:
 - was_top1_correct  ← the success metric for WS-2
 - was_in_top3       ← retrieval diagnostic
 - retrieval_failure ← right answer not in top-3 at all (cone/GPS problem)
 - ranking_failure   ← right answer in top-3 but not #1 (scoring problem)
"""

import logging
from typing import List

from services.analytics import track_confirmation

logger = logging.getLogger(__name__)


def log_confirmation(
    scan_id: str,
    top3_bins: List[str],
    confirmed_bin: str,
):
    was_top1_correct = bool(top3_bins) and top3_bins[0] == confirmed_bin
    was_in_top3 = confirmed_bin in top3_bins
    retrieval_failure = not was_in_top3
    ranking_failure = was_in_top3 and not was_top1_correct

    logger.debug(
        f"[telemetry:{scan_id}] confirm BIN={confirmed_bin} "
        f"top1_correct={was_top1_correct} in_top3={was_in_top3} "
        f"retrieval_fail={retrieval_failure} ranking_fail={ranking_failure}"
    )

    track_confirmation(scan_id, confirmed_bin, was_top_match=was_top1_correct)
