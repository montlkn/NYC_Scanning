"""Supabase user verification for routes that write on a user's behalf.

The rest of this API is anonymous by design (see utils/rate_limit.py). The
exception is anything that overwrites user content: /api/scan-photo used to
take a bare `scan_id` form field, so anyone who learned a scan id could replace
`contributions/{scan_id}.jpg` on R2 with any image they liked.

Verification asks the MAIN project's auth server (`GET /auth/v1/user`) instead
of checking the signature locally. Two reasons:
  - It works whichever signing scheme the project uses (legacy HS256 secret or
    asymmetric keys), so this code needs no JWT secret and does not break if
    the project migrates to signing keys.
  - It respects sign-out and deleted users, which a local signature check
    cannot see until the token expires.
The cost is one HTTP round trip per upload, and uploads are rare (explicit
opt-in per scan), so there is no cache.

Config: `SUPABASE_URL` and `SUPABASE_KEY` must point at MAIN
(gzzvhmmywaaxljpmoacm), where the app's users sign in. The key is only sent as
the gateway `apikey`; the user's own token is what gets verified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import HTTPException, Request

from models.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthUser:
    id: str
    is_anonymous: bool


def bearer_token(request: Request) -> Optional[str]:
    """The bearer token from the Authorization header, or None if absent."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Malformed Authorization header")
    return token.strip()


async def verify_supabase_token(token: str) -> AuthUser:
    """Resolve a Supabase access token to its user, or raise 401/503."""
    settings = get_settings()
    url = settings.supabase_url.rstrip("/") + "/auth/v1/user"
    headers = {"apikey": settings.supabase_key, "Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        logger.error(f"auth verify: Supabase unreachable: {e}")
        raise HTTPException(status_code=503, detail="Auth service unavailable")

    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if resp.status_code != 200:
        logger.error(f"auth verify: unexpected status {resp.status_code}")
        raise HTTPException(status_code=503, detail="Auth service unavailable")

    body = resp.json()
    user_id = body.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    return AuthUser(id=str(user_id), is_anonymous=bool(body.get("is_anonymous", False)))
