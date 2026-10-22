"""
app/auth/rate_limit.py — Login brute-force protection backed by Redis.

Strategy
--------
For every failed login attempt the key  login_fail:{email}  is incremented.
The key expires after `lockout_minutes` of inactivity (sliding window).
Once the counter reaches `max_attempts` the account is locked for the
remainder of that window.

On a successful login the counter is deleted immediately.
"""

import redis.asyncio as aioredis

from app.config import settings

_FAIL_PREFIX = "login_fail:"
_LOCK_PREFIX = "login_lock:"


async def record_failed_attempt(redis: aioredis.Redis, email: str) -> int:
    """
    Increment the failure counter.
    Returns the new count so the caller can decide whether to lock.
    """
    key = _FAIL_PREFIX + email.lower()
    lockout_secs = settings.login_lockout_minutes * 60

    # INCR + EXPIRE in a pipeline (atomic enough for this use-case)
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, lockout_secs)
    results = await pipe.execute()
    count: int = results[0]

    if count >= settings.login_max_attempts:
        # Set a dedicated lock key so we can check it without reading the counter
        lock_key = _LOCK_PREFIX + email.lower()
        await redis.set(lock_key, 1, ex=lockout_secs)

    return count


async def is_locked_out(redis: aioredis.Redis, email: str) -> bool:
    """Return True if the account is currently locked."""
    lock_key = _LOCK_PREFIX + email.lower()
    return await redis.exists(lock_key) == 1


async def clear_failed_attempts(redis: aioredis.Redis, email: str) -> None:
    """Call this after a successful login to reset the counter."""
    pipe = redis.pipeline()
    pipe.delete(_FAIL_PREFIX + email.lower())
    pipe.delete(_LOCK_PREFIX + email.lower())
    await pipe.execute()


async def remaining_lockout_seconds(redis: aioredis.Redis, email: str) -> int:
    """Return the TTL of the lock key (0 if not locked)."""
    lock_key = _LOCK_PREFIX + email.lower()
    ttl = await redis.ttl(lock_key)
    return max(ttl, 0)
