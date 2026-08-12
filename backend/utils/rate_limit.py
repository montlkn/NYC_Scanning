"""Per-IP rate limiting.

The scan API takes no authentication of any kind: every route is reachable by
anyone who knows the hostname. Several of them spend real money per call
(`/api/lore/{bin}` runs an LLM synthesis, `/api/search/sources` runs a billed
Brave fan-out) and the rest spend CPU or R2 storage. Without a limiter a single
script can drive that cost without bound. This module is the throttle.

It is deliberately IP-based rather than user-based: there is no identity to key
on. A `user_id` form field arrives on some routes but it is caller-supplied and
unverified, so keying on it would let an attacker mint a fresh bucket per
request.


Choosing the numbers
--------------------
The honest ceiling comes from what the real client does. Identification is
fully on-device now; the iOS app only touches this API to file bookkeeping for
a scan the user physically walked up to and took (`ScanAPIService.confirmScan`
-> POST /api/scans/{id}/confirm, plus an opt-in photo upload to
/api/scan-photo), and to fetch narrative/search for the building in front of
them. A person on foot produces a scan every 20-60 seconds at a sprint, so the
true human rate is ~2-3/min and a heavy day of walking the city is well under
200 scans.

The limits below sit above that, for one reason: **the key is an IP, and many
real users share one.** Mobile carriers put thousands of phones behind CGNAT,
and a cafe or an office is one NAT'd address. A limit tuned to a single walker
would lock out an entire carrier egress node. So the per-minute allowance is
loose enough to absorb a crowd of genuine users on one address, while the daily
cap is what actually bounds the spend — a scraper hits the daily wall long
before it has drained a budget, and no plausible group of humans behind one IP
approaches it.

Billed inference is capped tightest, cheap DB reads loosest.
"""

from __future__ import annotations

import os
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)


# --- Limit tiers -------------------------------------------------------------
# Read as: what a crowd behind one NAT could plausibly need per minute, and what
# no honest crowd would exceed per day.

# LLM synthesis + billed Brave queries. Every uncached call costs cents.
LIMIT_INFERENCE = ["20/minute", "300/day"]

# Scan bookkeeping: confirm + opt-in photo upload. One per building actually
# visited. Cheap individually, but the photo path writes to R2.
LIMIT_SCAN = ["30/minute", "600/day"]

# Embedding/vector search and plain DB reads. No per-call vendor cost, but they
# hold a DB connection and run the fastembed model, so they are not free either.
LIMIT_SEARCH = ["60/minute", "2000/day"]

# Everything not explicitly decorated. Wide enough that no normal screen trips
# it, narrow enough that an unauthenticated crawler cannot walk 35k buildings.
LIMIT_DEFAULT = ["120/minute", "5000/day"]


# --- Client IP behind Railway ------------------------------------------------
# Railway terminates TLS and proxies to the container, so `request.client.host`
# is a Railway internal address — identical for every user on Earth. Keying on
# it would mean one global bucket: the limiter would either do nothing useful or
# lock out the entire user base at once. The real address must come from
# X-Forwarded-For.
#
# X-Forwarded-For is client-controlled, so the LEFTMOST entry is not
# trustworthy — anyone can send `X-Forwarded-For: 1.2.3.4` and get a fresh
# bucket per request, which is worse than no limiter. The trustworthy value is
# the RIGHTMOST-but-N entry: each proxy in the chain APPENDS the peer it
# actually saw. With Railway's single edge hop in front of us, the last entry in
# the header is the address Railway's edge observed, i.e. the real client, and
# anything the client forged sits harmlessly to its left.
#
# TRUSTED_PROXY_HOPS = number of proxies between the internet and this process.
# 1 for stock Railway. Raise it only if you put another proxy (e.g. Cloudflare)
# in front, and lower it to 0 to fall back to the socket peer for local runs.
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))


def client_ip(request: Request) -> str:
    """The real client address, accounting for TRUSTED_PROXY_HOPS proxies."""
    if TRUSTED_PROXY_HOPS > 0:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                # Index from the right by the number of hops we trust. Clamp so
                # a short header (fewer hops than configured) degrades to the
                # leftmost entry rather than raising.
                idx = max(0, len(parts) - TRUSTED_PROXY_HOPS)
                return parts[idx]
    return get_remote_address(request)


# --- Limiter -----------------------------------------------------------------
# Uses Redis when REDIS_URL is present so the counters are shared across
# replicas; falls back to per-process memory otherwise. With N replicas and no
# Redis the effective limit is N times the configured one, which still bounds
# the damage but is not exact — set REDIS_URL in Railway if you scale out.
_storage_uri = os.getenv("REDIS_URL") or os.getenv("RATE_LIMIT_STORAGE_URI")
if _storage_uri:
    logger.info("[ratelimit] using shared storage backend")
else:
    logger.info("[ratelimit] no REDIS_URL — using in-memory counters (per-process)")

# Disable switch: RATE_LIMIT_ENABLED=false turns the whole thing off without a
# redeploy of code, for the case where a bad limit is locking out real users.
_enabled = os.getenv("RATE_LIMIT_ENABLED", "true").lower() not in ("0", "false", "no")

limiter = Limiter(
    key_func=client_ip,
    default_limits=LIMIT_DEFAULT,
    storage_uri=_storage_uri,
    enabled=_enabled,
    headers_enabled=True,  # emits X-RateLimit-* on every response
)


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """429 with Retry-After, in the same error envelope the app already uses."""
    # Retry-After is the full window of the limit that tripped (60 for a
    # "/minute" rule, 86400 for a "/day" rule). slowapi does not expose the
    # exact reset instant on the exception, so this is an upper bound: waiting
    # this long is always sufficient. `X-RateLimit-Reset` (emitted by
    # headers_enabled) carries the precise timestamp for clients that want it.
    retry_after = 60
    try:
        retry_after = max(1, int(exc.limit.limit.get_expiry()))
    except Exception:
        pass

    ip = client_ip(request)
    logger.warning(f"[ratelimit] 429 {request.method} {request.url.path} ip={ip}")
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limited",
            "message": "Too many requests. Try again shortly.",
        },
        headers={"Retry-After": str(retry_after)},
    )
