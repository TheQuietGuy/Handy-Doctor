"""
tests/conftest.py — Session-scoped test environment setup.

Fixes two environment issues:
1. RSA keys: generate a temporary key pair so JWT works without running openssl.
2. bcrypt: the installed bcrypt library's self-test hashes a string longer than
   72 bytes which it then rejects. Patch bcrypt.hashpw to truncate before the
   self-test runs so passlib can load the backend cleanly.
3. Redis in app.state: inject a mock into app.state.redis so the rate-limit
   middleware doesn't error out during tests.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# ── 1. RSA keys ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    root = Path(__file__).parent.parent
    (root / "private.pem").write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (root / "public.pem").write_bytes(
        public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    (root / "private.pem").unlink(missing_ok=True)
    (root / "public.pem").unlink(missing_ok=True)
    get_settings.cache_clear()


# ── 2. bcrypt 72-byte self-test patch ────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def patch_bcrypt_hashpw():
    """
    Patch bcrypt.hashpw to silently truncate passwords > 72 bytes.
    This fixes the passlib backend self-test which uses a 200-byte string.
    Must run BEFORE any passlib import tries to load the bcrypt backend.
    """
    import bcrypt as _bcrypt_lib

    original_hashpw = _bcrypt_lib.hashpw

    def _safe_hashpw(password, salt):
        if isinstance(password, (bytes, bytearray)) and len(password) > 72:
            password = password[:72]
        return original_hashpw(password, salt)

    _bcrypt_lib.hashpw = _safe_hashpw
    yield
    _bcrypt_lib.hashpw = original_hashpw


# ── 3. Redis in app.state ────────────────────────────────────────────────────
def _make_redis_mock():
    m = AsyncMock()
    pipe = AsyncMock()
    pipe.execute = AsyncMock(return_value=[0, 0, None, True, []])
    pipe.zremrangebyscore = MagicMock(return_value=None)
    pipe.zcard = MagicMock(return_value=None)
    pipe.zadd = MagicMock(return_value=None)
    pipe.expire = MagicMock(return_value=None)
    pipe.zrange = MagicMock(return_value=None)
    m.pipeline = MagicMock(return_value=pipe)
    m.exists = AsyncMock(return_value=0)
    m.incr = AsyncMock(return_value=1)
    m.expire = AsyncMock(return_value=True)
    m.set = AsyncMock(return_value=True)
    m.get = AsyncMock(return_value=None)
    m.delete = AsyncMock(return_value=1)
    m.ttl = AsyncMock(return_value=-2)
    return m


@pytest.fixture(autouse=True)
def inject_redis_into_app_state(request):
    if "client" not in request.fixturenames:
        yield
        return
    from main import app

    app.state.redis = _make_redis_mock()
    yield
    try:
        del app.state.redis
    except AttributeError:
        pass
