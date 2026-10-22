"""
app/auth/oauth.py — Google OAuth 2.0 (Authorization Code + PKCE).

Flow summary
------------
1. /oauth/google/initiate  — generate code_verifier, code_challenge, state;
   store both in Redis (TTL 10 min); redirect to Google.
2. /oauth/google/callback  — verify state from Redis, exchange code for
   tokens, fetch user-info, upsert User row, issue our own JWT pair.
"""

import base64
import hashlib
import secrets

import httpx
import redis.asyncio as aioredis

from app.config import settings

_STATE_PREFIX = "oauth_state:"
_STATE_TTL = 600  # 10 minutes

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = "openid email profile"


# ── PKCE helpers ─────────────────────────────────────────────────────────────
def _generate_code_verifier() -> str:
    """43–128 char URL-safe random string."""
    return secrets.token_urlsafe(48)


def _code_challenge(verifier: str) -> str:
    """S256 code challenge: BASE64URL(SHA256(verifier))."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ── Step 1: build redirect URL ────────────────────────────────────────────────
async def build_google_auth_url(redis: aioredis.Redis) -> str:
    """
    Generates PKCE params and state, persists them to Redis, and returns
    the full Google authorisation URL to redirect the user to.
    """
    state = secrets.token_urlsafe(32)
    verifier = _generate_code_verifier()
    challenge = _code_challenge(verifier)

    # Persist so we can validate on callback
    await redis.set(
        _STATE_PREFIX + state,
        verifier,
        ex=_STATE_TTL,
    )

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "select_account",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{GOOGLE_AUTH_URL}?{query}"


# ── Step 2: exchange code for user info ───────────────────────────────────────
class OAuthUserInfo:
    def __init__(self, sub: str, email: str, name: str, avatar_url: str | None = None):
        self.sub = sub
        self.email = email
        self.name = name
        self.avatar_url = avatar_url


async def exchange_google_code(redis: aioredis.Redis, code: str, state: str) -> OAuthUserInfo:
    """
    Validates state, retrieves verifier from Redis, exchanges the code for
    tokens, and fetches the Google user-info endpoint.

    Raises ValueError on any CSRF / validation failure.
    """
    # 1. Retrieve code_verifier — also validates state (CSRF check)
    verifier = await redis.get(_STATE_PREFIX + state)
    if not verifier:
        raise ValueError("Invalid or expired OAuth state. Please try again.")
    await redis.delete(_STATE_PREFIX + state)

    # 2. Token exchange
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()

        # 3. Fetch user info
        info_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        info_resp.raise_for_status()
        info = info_resp.json()

    return OAuthUserInfo(
        sub=info["sub"],
        email=info["email"],
        name=info.get("name", ""),
        avatar_url=info.get("picture"),
    )
