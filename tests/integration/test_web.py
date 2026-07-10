"""Live SearXNG and Redis-cache checks, skipped when local services are absent."""

from __future__ import annotations

import hashlib

import httpx
import pytest
from redis.asyncio import Redis

from tools.web import WebSearchArgs, _web_search

SEARXNG_URL = "http://127.0.0.1:8091"
CACHE_URL = "redis://127.0.0.1:6380/0"


async def _services_or_skip() -> Redis:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{SEARXNG_URL}/search", params={"q": "SearXNG", "format": "json"}
            )
        if response.status_code != 200:
            pytest.skip(f"local SearXNG returned {response.status_code}")
        cache = Redis.from_url(CACHE_URL, decode_responses=True)
        await cache.ping()
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        pytest.skip(f"local web infrastructure is not reachable: {exc}")
    return cache


@pytest.mark.integration
async def test_live_searxng_returns_results() -> None:
    cache = await _services_or_skip()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                f"{SEARXNG_URL}/search", params={"q": "SearXNG", "format": "json"}
            )
        body = response.json()
        assert isinstance(body["results"], list)
        assert len(body["results"]) > 0
    finally:
        await cache.aclose()


@pytest.mark.integration
async def test_live_web_search_caches_a_result() -> None:
    cache = await _services_or_skip()
    query = "SearXNG open source search"
    key = f"websearch:{hashlib.sha256(query.lower().encode()).hexdigest()}"
    try:
        await cache.delete(key)
        result = await _web_search(cache, SEARXNG_URL, WebSearchArgs(query=query))

        assert result.status == "ok"
        assert result.payload["results"]
        assert await cache.get(key) is not None
    finally:
        await cache.aclose()
