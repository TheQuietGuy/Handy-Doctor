"""
app/auth/jwt.py — RS256 JWT issuance and verification.

Token design
------------
• Access token  — RS256 JWT, 15-minute lifetime, returned in JSON body.
• Refresh token — 256-bit random secret, 7-day lifetime, stored as SHA-256
  hash in Postgres *and* sent to the client as an HttpOnly Secure cookie.
  On every use the old row is deleted and a new token is issued (rotation).
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from app.config import settings


# ── Access tokens ────────────────────────────────────────────────────────────
def create_access_token(user_id: uuid.UUID) -> str:
    """Issue a short-lived RS256 JWT."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_private_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT.
    Raises jose.JWTError on any failure (expired, bad signature, wrong type).
    """
    payload = jwt.decode(
        token,
        settings.jwt_public_key,
        algorithms=[settings.jwt_algorithm],
    )
    if payload.get("type") != "access":
        raise JWTError("Token type is not 'access'.")
    return payload


def get_user_id_from_token(token: str) -> uuid.UUID:
    """Convenience wrapper — returns the UUID from a valid access token."""
    payload = decode_access_token(token)
    return uuid.UUID(payload["sub"])


# ── Refresh tokens ───────────────────────────────────────────────────────────
def generate_refresh_token() -> tuple[str, str]:
    """
    Return (raw_token, token_hash).
    raw_token  — sent to the client (HttpOnly cookie).
    token_hash — stored in the database.
    """
    raw = secrets.token_urlsafe(32)  # 256 bits of entropy
    hashed = _hash_token(raw)
    return raw, hashed


def hash_refresh_token(raw: str) -> str:
    return _hash_token(raw)


def refresh_token_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)


# ── Email / password-reset tokens ────────────────────────────────────────────
def generate_url_token() -> tuple[str, str]:
    """
    Return (raw_token, token_hash) suitable for email links.
    Uses 32 random bytes → 43-char URL-safe base64 string.
    """
    raw = secrets.token_urlsafe(32)
    hashed = _hash_token(raw)
    return raw, hashed


# ── Internal ─────────────────────────────────────────────────────────────────
def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
