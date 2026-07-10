"""Offline coverage for web search and protected page fetching."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from tools.web import (
    MAX_PAGE_BYTES,
    FetchPageArgs,
    WebSearchArgs,
    _fetch_page,
    _web_search,
)


class FakeCacheRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(self, name: str, value: str, *, ex: int | None = None) -> bool:
        self.values[name] = value
        if ex is not None:
            self.expiries[name] = ex
        return True


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_web_search_cache_hit_skips_http_and_slices_results() -> None:
    cache = FakeCacheRedis()
    query = "  Latest   News "
    normalized = "latest news"
    key = f"websearch:{hashlib.sha256(normalized.encode()).hexdigest()}"
    cache.values[key] = json.dumps(
        [
            {"title": "One", "url": "https://one.example", "snippet": "First"},
            {"title": "Two", "url": "https://two.example", "snippet": "Second"},
        ]
    )
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    result = await _web_search(
        cache,
        "http://searxng.test",
        WebSearchArgs(query=query, num_results=1),
        transport=_transport(handler),
    )

    assert result.payload["results"] == [
        {"title": "One", "url": "https://one.example", "snippet": "First"}
    ]
    assert calls == 0


async def test_web_search_maps_results_caps_and_caches_for_one_hour() -> None:
    cache = FakeCacheRedis()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params == httpx.QueryParams({"q": "Kittens", "format": "json"})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": f"Result {index}",
                        "url": f"https://{index}.example",
                        "content": "Text",
                    }
                    for index in range(10)
                ]
            },
        )

    result = await _web_search(
        cache,
        "http://searxng.test",
        WebSearchArgs(query="Kittens", num_results=3),
        transport=_transport(handler),
    )
    key = f"websearch:{hashlib.sha256(b'kittens').hexdigest()}"

    assert len(result.payload["results"]) == 3
    assert len(json.loads(cache.values[key])) == 8
    assert cache.expiries[key] == 3_600


@pytest.mark.parametrize("failure", ["server", "connect"])
async def test_web_search_maps_service_failures_to_non_retryable_error(failure: str) -> None:
    cache = FakeCacheRedis()

    async def handler(_request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("offline")
        return httpx.Response(500)

    result = await _web_search(
        cache,
        "http://searxng.test",
        WebSearchArgs(query="news"),
        transport=_transport(handler),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert result.error == "Поиск временно недоступен. Попробуйте позже."


async def test_fetch_page_rejects_private_redirect_target() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    result = await _fetch_page(
        FetchPageArgs(url="https://93.184.216.34/start"), transport=_transport(handler)
    )

    assert result.status == "error"
    assert result.error == "Этот адрес запрашивать нельзя."


async def test_fetch_page_rejects_more_than_five_redirects() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        current = int(request.url.path.removeprefix("/redirect"))
        return httpx.Response(302, headers={"location": f"/redirect{current + 1}"})

    result = await _fetch_page(
        FetchPageArgs(url="https://93.184.216.34/redirect0"), transport=_transport(handler)
    )

    assert result.status == "error"
    assert result.error == "Слишком много перенаправлений."


async def test_fetch_page_aborts_download_above_five_mebibytes() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * (MAX_PAGE_BYTES + 1),
        )

    result = await _fetch_page(
        FetchPageArgs(url="https://93.184.216.34/large"), transport=_transport(handler)
    )

    assert result.status == "error"
    assert result.error == "Страница слишком большая"


async def test_fetch_page_rejects_non_text_and_empty_extraction() -> None:
    async def binary_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    binary = await _fetch_page(
        FetchPageArgs(url="https://93.184.216.34/file"), transport=_transport(binary_handler)
    )

    async def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html><body></body></html>"
        )

    empty = await _fetch_page(
        FetchPageArgs(url="https://93.184.216.34/empty"), transport=_transport(empty_handler)
    )

    assert binary.status == empty.status == "error"
    assert empty.error == "Не удалось извлечь текст страницы"


async def test_fetch_page_extracts_article_text() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="""
            <html><body><article><h1>Useful article</h1>
            <p>This article contains enough readable text for extraction.</p>
            <p>It has a second paragraph with additional details.</p>
            </article></body></html>
            """,
        )

    result = await _fetch_page(
        FetchPageArgs(url="https://93.184.216.34/article"), transport=_transport(handler)
    )

    assert result.status == "ok"
    assert result.payload["url"] == "https://93.184.216.34/article"
    assert "Useful article" in str(result.payload["text"])
