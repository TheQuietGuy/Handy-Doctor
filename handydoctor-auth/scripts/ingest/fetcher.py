"""scripts/ingest/fetcher.py -- async HTTP fetcher."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)
POLITENESS_DELAY = 1.0
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30.0
USER_AGENT = "HandyDoctorBot/1.0 (medical triage RAG pipeline)"

_last_request: dict[str, float] = defaultdict(float)
_domain_lock: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def fetch(url: str) -> bytes:
    hostname = urlparse(url).hostname or url
    lock = _domain_lock[hostname]
    for attempt in range(1, MAX_RETRIES + 1):
        async with lock:
            elapsed = time.monotonic() - _last_request[hostname]
            if elapsed < POLITENESS_DELAY:
                await asyncio.sleep(POLITENESS_DELAY - elapsed)
            try:
                async with httpx.AsyncClient(
                    headers={"User-Agent": USER_AGENT},
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                ) as client:
                    response = await client.get(url)
                    _last_request[hostname] = time.monotonic()
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", "60"))
                        await asyncio.sleep(retry_after)
                        continue
                    response.raise_for_status()
                    return response.content
            except httpx.TimeoutException:
                logger.warning("timeout on attempt %d/%d: %s", attempt, MAX_RETRIES, url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2**attempt)
    raise RuntimeError(f"All {MAX_RETRIES} attempts failed for {url}")
