"""
app/middleware/rate_limit.py — Sliding-window rate limiter middleware.

Algorithm
─────────
Uses a Redis sorted set per (scope, IP) pair.

  Key:    rl:{scope}:{ip}          e.g.  rl:auth_strict:203.0.113.42
  Score:  current Unix timestamp in milliseconds
  Member: unique request ID (UUID4 hex — prevents score collisions)

On every request the pipeline atomically:
  1. Removes members with score < (now - window_ms)   [expire old entries]
  2. Counts remaining members                          [current usage]
  3. Adds the new member with score = now              [record this request]
  4. Sets the key TTL = window_secs + 1               [auto-cleanup]

If count (before adding) >= limit -> 429, else -> pass.

Headers set on every response
──────────────────────────────
  X-RateLimit-Scope      auth_strict / auth_standard / account_write / ...
  X-RateLimit-Limit      max_requests for the matched policy
  X-RateLimit-Remaining  requests left in the current window (floor 0)
  X-RateLimit-Reset      Unix timestamp (seconds) when the window resets
  Retry-After            seconds to wait (only on 429 responses)
"""

import logging
import time
import uuid

import redis.asyncio as aioredis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.middleware.config import RatePolicy, get_policy
from app.middleware.ip import extract_client_ip

logger = logging.getLogger(__name__)

_EXEMPT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that enforces per-IP sliding-window rate limits.

    Redis is read from request.app.state.redis at request time so the
    middleware can be registered before the lifespan startup block runs.
    Pass redis=None at construction; the lifespan sets app.state.redis.
    """

    def __init__(self, app: ASGIApp, redis: aioredis.Redis | None = None) -> None:
        super().__init__(app)
        self._redis_override = redis  # only used in tests

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        # Prefer test-injected redis, then app.state, then skip (fail open)
        redis = (
            self._redis_override
            or getattr(getattr(request, "app", None), "state", None)
            and request.app.state.redis
        )

        if redis is None:
            logger.warning("Rate limiter: no Redis client available — failing open")
            return await call_next(request)

        policy = get_policy(path)
        ip = extract_client_ip(request)

        allowed, current_count, reset_at = await _check_sliding_window(redis, ip, policy)

        remaining = max(policy.max_requests - current_count, 0)

        if not allowed:
            retry_after = max(int(reset_at - time.time()), 1)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"Rate limit exceeded for scope '{policy.scope}'. "
                        f"Try again in {retry_after} seconds."
                    ),
                    "retry_after": retry_after,
                },
                headers=_rate_headers(policy, 0, reset_at, retry_after),
            )

        response = await call_next(request)

        for key, val in _rate_headers(policy, remaining, reset_at).items():
            response.headers[key] = val

        return response


async def _check_sliding_window(
    redis: aioredis.Redis,
    ip: str,
    policy: RatePolicy,
) -> tuple[bool, int, float]:
    """
    Execute the atomic sliding-window pipeline.

    Returns (allowed, count_after_this_request, reset_unix_timestamp).
    """
    now_ms = int(time.time() * 1000)
    window_ms = policy.window_secs * 1000
    cutoff_ms = now_ms - window_ms
    key = f"rl:{policy.scope}:{ip}"
    member = uuid.uuid4().hex
    reset_at = time.time() + policy.window_secs

    try:
        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, "-inf", cutoff_ms)
        pipe.zcard(key)
        pipe.zadd(key, {member: now_ms})
        pipe.expire(key, policy.window_secs + 1)
        results = await pipe.execute()

        count_before: int = results[1]
        count_after = count_before + 1
        allowed = count_before < policy.max_requests
        return allowed, count_after, reset_at

    except Exception:
        logger.exception("Rate limiter Redis error for key %r — failing open", key)
        return True, 0, reset_at


def _rate_headers(
    policy: RatePolicy,
    remaining: int,
    reset_at: float,
    retry_after: int | None = None,
) -> dict[str, str]:
    headers = {
        "X-RateLimit-Scope": policy.scope,
        "X-RateLimit-Limit": str(policy.max_requests),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(int(reset_at)),
    }
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return headers


async def check_rate_limit(
    redis: aioredis.Redis,
    ip: str,
    policy: RatePolicy,
) -> None:
    """
    Programmatic rate-limit check for use inside route handlers.
    Raises HTTPException(429) if the limit is exceeded.
    """
    from fastapi import HTTPException, status

    allowed, _, reset_at = await _check_sliding_window(redis, ip, policy)
    if not allowed:
        retry_after = max(int(reset_at - time.time()), 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
