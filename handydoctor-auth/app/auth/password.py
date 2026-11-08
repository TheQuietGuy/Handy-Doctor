"""
app/auth/password.py - bcrypt hashing (cost factor 12) and verification.

Uses the bcrypt library directly (passlib is unmaintained and breaks on
bcrypt >= 4.1/5.x). Hashes remain standard $2b$ strings.
"""

import bcrypt

_ROUNDS = 12
_MAX_BYTES = 72  # bcrypt ignores everything past 72 bytes


def _prep(plain: str) -> bytes:
    return plain.encode("utf-8")[:_MAX_BYTES]


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return bcrypt.hashpw(_prep(plain), bcrypt.gensalt(rounds=_ROUNDS)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*; False otherwise."""
    try:
        return bcrypt.checkpw(_prep(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
