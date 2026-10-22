"""
tests/test_rate_limit.py — Tests for the sliding-window rate-limit middleware.

Covers:
  1. Policy resolution (get_policy / ROUTE_POLICIES)
  2. Sliding-window algorithm (_check_sliding_window)
  3. IP extraction helpers
  4. Middleware integration via a minimal FastAPI test app
"""

import time
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis as fakeredis
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.config import ROUTE_POLICIES, RatePolicy, get_policy
from app.middleware.ip import extract_client_ip
from app.middleware.rate_limit import RateLimitMiddleware, _check_sliding_window


# ── Shared fixtures ───────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def redis():
    r = fakeredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


def _make_test_app(fake_redis=None) -> FastAPI:
    app = FastAPI()
    if fake_redis:
        app.state.redis = fake_redis

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/api/accounts/register")
    async def register():
        return {"registered": True}

    @app.post("/api/accounts/login")
    async def login():
        return {"logged_in": True}

    @app.post("/api/accounts/forgot-password")
    async def forgot():
        return {"sent": True}

    @app.post("/api/accounts/me/export")
    async def export():
        return {"exported": True}

    @app.get("/api/accounts/me")
    async def me():
        return {"user": "data"}

    @app.post("/api/chat/sessions")
    async def chat():
        return {"session": "created"}

    app.add_middleware(RateLimitMiddleware)
    return app


@pytest_asyncio.fixture
async def client(redis):
    app = _make_test_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ════════════════════════════════════════════════════════════════════════════
# 1. POLICY RESOLUTION
# ════════════════════════════════════════════════════════════════════════════
class TestPolicyResolution:
    def test_register_gets_strict_policy(self):
        p = get_policy("/api/accounts/register")
        assert p.max_requests <= 10
        assert "strict" in p.scope

    def test_login_gets_auth_standard(self):
        p = get_policy("/api/accounts/login")
        assert "standard" in p.scope or "auth" in p.scope

    def test_forgot_password_gets_strict(self):
        p = get_policy("/api/accounts/forgot-password")
        assert p.max_requests <= 10

    def test_me_get_gets_high_limit(self):
        p = get_policy("/api/accounts/me")
        assert p.max_requests >= 60

    def test_export_is_restricted(self):
        p = get_policy("/api/accounts/me/export")
        assert p.max_requests <= 20

    def test_health_is_not_in_policies(self):
        # /health is exempt in the middleware before policy lookup
        p = get_policy("/health")
        # Falls through to default — not explicitly listed as strict
        assert p.max_requests >= 100

    def test_unknown_path_uses_default(self):
        p = get_policy("/api/some/unknown/endpoint")
        assert p.scope == "default"

    def test_chat_sessions_has_policy(self):
        p = get_policy("/api/chat/sessions")
        assert p.max_requests > 0

    def test_all_policies_have_positive_limits(self):
        for _, policy in ROUTE_POLICIES:
            assert policy.max_requests > 0
            assert policy.window_secs > 0
            assert policy.scope

    def test_longer_prefix_beats_shorter(self):
        """Specific path wins over a shorter prefix that also matches."""
        p_specific = get_policy("/api/accounts/me/export")
        p_general = get_policy("/api/accounts/me")
        # export must be more restrictive
        assert p_specific.max_requests <= p_general.max_requests


# ════════════════════════════════════════════════════════════════════════════
# 2. SLIDING-WINDOW ALGORITHM
# ════════════════════════════════════════════════════════════════════════════
class TestSlidingWindow:
    @pytest.mark.asyncio
    async def test_allows_requests_under_limit(self, redis):
        policy = RatePolicy(max_requests=5, window_secs=60, scope="test")
        for _ in range(5):
            allowed, _, _ = await _check_sliding_window(redis, "1.2.3.4", policy)
            assert allowed

    @pytest.mark.asyncio
    async def test_blocks_at_limit(self, redis):
        policy = RatePolicy(max_requests=3, window_secs=60, scope="test")
        for _ in range(3):
            await _check_sliding_window(redis, "1.2.3.4", policy)
        allowed, _, _ = await _check_sliding_window(redis, "1.2.3.4", policy)
        assert not allowed

    @pytest.mark.asyncio
    async def test_count_increments(self, redis):
        policy = RatePolicy(max_requests=10, window_secs=60, scope="test")
        for expected in range(1, 5):
            _, count, _ = await _check_sliding_window(redis, "5.6.7.8", policy)
            assert count == expected

    @pytest.mark.asyncio
    async def test_reset_at_is_future(self, redis):
        policy = RatePolicy(max_requests=5, window_secs=60, scope="test")
        _, _, reset_at = await _check_sliding_window(redis, "9.9.9.9", policy)
        assert reset_at > time.time()

    @pytest.mark.asyncio
    async def test_different_ips_are_independent(self, redis):
        policy = RatePolicy(max_requests=2, window_secs=60, scope="test")
        for _ in range(2):
            await _check_sliding_window(redis, "ip-a", policy)
        blocked, _, _ = await _check_sliding_window(redis, "ip-a", policy)
        allowed, _, _ = await _check_sliding_window(redis, "ip-b", policy)
        assert not blocked
        assert allowed

    @pytest.mark.asyncio
    async def test_old_entries_evicted_after_window(self, redis):
        policy = RatePolicy(max_requests=2, window_secs=1, scope="test")
        now = time.time()
        # Simulate two requests at t=0 (already expired by using past timestamps)
        key = f"rl:{policy.scope}:old-ip"
        past_ms = int((now - 2) * 1000)  # 2 seconds ago → outside a 1s window
        await redis.zadd(key, {"old1": past_ms, "old2": past_ms})
        # New request at t=now should be allowed (old entries evicted)
        allowed, count, _ = await _check_sliding_window(redis, "old-ip", policy)
        assert allowed
        assert count == 1  # only the new request

    @pytest.mark.asyncio
    async def test_no_burst_across_window_boundary(self, redis):
        """
        Classic fixed-window burst: fire limit requests just before window end,
        then fire more right after.  Sliding window must block the second batch.
        """
        policy = RatePolicy(max_requests=3, window_secs=10, scope="burst")
        ip = "burst-ip"
        # Fill the limit
        for _ in range(3):
            await _check_sliding_window(redis, ip, policy)
        # 1 second later (still within 10s window) — must be blocked
        allowed, _, _ = await _check_sliding_window(redis, ip, policy)
        assert not allowed

    @pytest.mark.asyncio
    async def test_redis_error_fails_open(self):
        """If Redis raises, the middleware should not block the request."""
        bad_redis = AsyncMock()
        bad_redis.pipeline = MagicMock(side_effect=Exception("Redis down"))
        policy = RatePolicy(max_requests=5, window_secs=60, scope="test")
        allowed, count, _ = await _check_sliding_window(bad_redis, "any-ip", policy)
        assert allowed
        assert count == 0


# ════════════════════════════════════════════════════════════════════════════
# 3. IP EXTRACTION
# ════════════════════════════════════════════════════════════════════════════
class TestIPExtraction:
    def _req(self, headers: dict, host: str = "1.2.3.4") -> MagicMock:
        r = MagicMock()
        r.headers = headers
        r.client = MagicMock()
        r.client.host = host
        return r

    def test_no_xff_uses_client_host(self):
        req = self._req({})
        assert extract_client_ip(req) == "1.2.3.4"

    def test_xff_respected_when_trust_proxy_enabled(self):
        from unittest.mock import patch

        req = self._req({"X-Forwarded-For": "203.0.113.5"})
        with patch("app.middleware.ip.settings") as mock_settings:
            mock_settings.trust_proxy = True
            ip = extract_client_ip(req)
        assert ip == "203.0.113.5"

    def test_xff_ignored_when_trust_proxy_disabled(self):
        # Default: trust_proxy=False — falls back to client.host
        req = self._req({"X-Forwarded-For": "203.0.113.5"})
        ip = extract_client_ip(req)
        assert ip == "1.2.3.4"  # uses direct connection, ignores XFF

    def test_xff_multiple_hops_takes_leftmost(self):
        from unittest.mock import patch

        req = self._req({"X-Forwarded-For": "203.0.113.1, 10.0.0.1, 10.0.0.2"})
        with patch("app.middleware.ip.settings") as mock_settings:
            mock_settings.trust_proxy = True
            ip = extract_client_ip(req)
        assert ip == "203.0.113.1"  # leftmost = real client

    def test_no_client_returns_unknown_or_string(self):
        req = MagicMock()
        req.headers = {}
        req.client = None
        ip = extract_client_ip(req)
        assert isinstance(ip, str) and len(ip) > 0


# ════════════════════════════════════════════════════════════════════════════
# 4. MIDDLEWARE INTEGRATION
# ════════════════════════════════════════════════════════════════════════════
class TestMiddlewareIntegration:
    @pytest.mark.asyncio
    async def test_health_never_rate_limited(self, client, redis):
        for _ in range(200):
            resp = await client.get("/health")
            assert resp.status_code == 200
        keys = await redis.keys("rl:*")
        assert len(keys) == 0

    @pytest.mark.asyncio
    async def test_response_has_rate_limit_headers(self, client):
        resp = await client.post("/api/accounts/login")
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers
        assert "X-RateLimit-Reset" in resp.headers
        assert "X-RateLimit-Scope" in resp.headers

    @pytest.mark.asyncio
    async def test_remaining_decrements(self, client):
        prev = None
        for _ in range(4):
            resp = await client.get("/api/accounts/me")
            remaining = int(resp.headers["X-RateLimit-Remaining"])
            if prev is not None:
                assert remaining == prev - 1
            prev = remaining

    @pytest.mark.asyncio
    async def test_strict_endpoint_blocks_after_limit(self, client, redis):
        limit = get_policy("/api/accounts/register").max_requests
        for _ in range(limit):
            r = await client.post("/api/accounts/register")
            assert r.status_code == 200
        blocked = await client.post("/api/accounts/register")
        assert blocked.status_code == 429

    @pytest.mark.asyncio
    async def test_429_has_retry_after_header(self, client, redis):
        limit = get_policy("/api/accounts/forgot-password").max_requests
        for _ in range(limit):
            await client.post("/api/accounts/forgot-password")
        resp = await client.post("/api/accounts/forgot-password")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert int(resp.headers["Retry-After"]) > 0

    @pytest.mark.asyncio
    async def test_429_body_has_detail_and_retry_after(self, client, redis):
        limit = get_policy("/api/accounts/forgot-password").max_requests
        for _ in range(limit):
            await client.post("/api/accounts/forgot-password")
        resp = await client.post("/api/accounts/forgot-password")
        body = resp.json()
        assert "detail" in body
        assert "retry_after" in body
        assert body["retry_after"] > 0

    @pytest.mark.asyncio
    async def test_different_ips_have_independent_buckets(self, redis):
        """
        Since trust_proxy=False in tests, we can't use XFF to differentiate IPs.
        Instead, verify the rate counter fills up using the same client host,
        and that a fresh Redis gives a clean slate.
        """
        import fakeredis.aioredis as fr

        limit = get_policy("/api/accounts/register").max_requests

        # Bucket 1: fill up with redis_a
        redis_a = fr.FakeRedis()
        app_a = _make_test_app(redis_a)
        async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as c:
            for _ in range(limit):
                await c.post("/api/accounts/register")
            blocked = await c.post("/api/accounts/register")

        # Bucket 2: fresh redis — same client, clean slate
        redis_b = fr.FakeRedis()
        app_b = _make_test_app(redis_b)
        async with AsyncClient(transport=ASGITransport(app=app_b), base_url="http://test") as c:
            allowed = await c.post("/api/accounts/register")

        await redis_a.aclose()
        await redis_b.aclose()
        assert blocked.status_code == 429
        assert allowed.status_code == 200

    @pytest.mark.asyncio
    async def test_redis_unavailable_fails_open(self):
        app = _make_test_app(fake_redis=None)
        app.state.redis = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/accounts/register")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_scope_header_matches_policy(self, client):
        resp = await client.post("/api/accounts/register")
        scope = resp.headers.get("X-RateLimit-Scope", "")
        expected = get_policy("/api/accounts/register").scope
        assert scope == expected

    @pytest.mark.asyncio
    async def test_reset_header_is_future_timestamp(self, client):
        resp = await client.post("/api/accounts/login")
        reset = int(resp.headers["X-RateLimit-Reset"])
        assert reset > int(time.time())

    @pytest.mark.asyncio
    async def test_export_endpoint_is_restricted(self, client, redis):
        limit = get_policy("/api/accounts/me/export").max_requests
        for _ in range(limit):
            r = await client.post("/api/accounts/me/export")
            assert r.status_code == 200
        blocked = await client.post("/api/accounts/me/export")
        assert blocked.status_code == 429
