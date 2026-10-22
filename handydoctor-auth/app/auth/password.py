"""
app/auth/password.py — bcrypt hashing (cost factor 12) and verification.
"""

from passlib.context import CryptContext

# bcrypt at cost-factor 12 (plan §3.3)
_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return _ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*; False otherwise."""
    return _ctx.verify(plain, hashed)
