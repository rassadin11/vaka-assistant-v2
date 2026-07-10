"""Internal SearXNG search and SSRF-protected page retrieval tools."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx
import trafilatura
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.ssrf import SsrfBlockedError, validate_public_url
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

SEARCH_CACHE_TTL_SECONDS = 3_600
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5


class CacheRedis(Protocol):
    """Subset of Redis used by the web-search cache."""

    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: str, *, ex: int | None = None) -> Any: ...


class WebSearchArgs(BaseModel):
    """Arguments exposed to the language model for web search."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=200)
    num_results: int = Field(default=5, ge=1, le=8)


class FetchPageArgs(BaseModel):
    """Arguments exposed to the language model for page retrieval."""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(max_length=2_000)


def register_web_tools(registry: ToolRegistry, cache_redis: CacheRedis, searxng_url: str) -> None:
    """Register web tools with their process-level service dependencies."""

    async def web_search(context: TaskContext, args: BaseModel) -> ToolResult:
        del context
        return await _web_search(cache_redis, searxng_url, _as_web_search_args(args))

    async def fetch_page(context: TaskContext, args: BaseModel) -> ToolResult:
        del context
        return await _fetch_page(_as_fetch_page_args(args))

    registry.register(
        ToolSpec(
            name="web_search",
            description="Search the public web and return concise result snippets.",
            args_schema=WebSearchArgs,
            risk=RiskLevel.READ_ONLY,
            handler=web_search,
            daily_limit=50,
        )
    )
    registry.register(
        ToolSpec(
            name="fetch_page",
            description="Fetch and extract readable text from a public web page URL.",
            args_schema=FetchPageArgs,
            risk=RiskLevel.READ_ONLY,
            handler=fetch_page,
            daily_limit=30,
        )
    )


async def _web_search(
    cache_redis: CacheRedis,
    searxng_url: str,
    args: WebSearchArgs,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolResult:
    normalized = " ".join(args.query.lower().split())
    key = f"websearch:{hashlib.sha256(normalized.encode()).hexdigest()}"
    cached = await cache_redis.get(key)
    cached_rows = _cached_rows(cached)
    if cached_rows is not None:
        return ToolResult(status="ok", payload={"results": cached_rows[: args.num_results]})

    try:
        async with httpx.AsyncClient(
            base_url=searxng_url.rstrip("/"), timeout=8.0, transport=transport
        ) as client:
            response = await client.get("/search", params={"q": args.query, "format": "json"})
        if response.status_code != 200:
            return _search_unavailable()
        rows = _search_rows(response.json())
    except (httpx.HTTPError, ValueError, TypeError):
        return _search_unavailable()

    await cache_redis.set(
        key,
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        ex=SEARCH_CACHE_TTL_SECONDS,
    )
    return ToolResult(status="ok", payload={"results": rows[: args.num_results]})


async def _fetch_page(
    args: FetchPageArgs,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolResult:
    try:
        # getaddrinfo is blocking: keep DNS lookups off the worker event loop.
        await asyncio.to_thread(validate_public_url, args.url)
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=httpx.Timeout(10.0), transport=transport
        ) as client:
            async with asyncio.timeout(10.0):
                final_url, html = await _download_page(client, args.url)
    except SsrfBlockedError:
        return ToolResult(status="error", error="Этот адрес запрашивать нельзя.")
    except _PageTooLargeError:
        return ToolResult(status="error", error="Страница слишком большая")
    except _UnsupportedContentTypeError:
        return ToolResult(status="error", error="Поддерживаются только текстовые страницы.")
    except _TooManyRedirectsError:
        return ToolResult(status="error", error="Слишком много перенаправлений.")
    except (httpx.HTTPError, TimeoutError, ValueError):
        return ToolResult(status="error", error="Не удалось загрузить страницу.")

    text = trafilatura.extract(html)
    if not text or not text.strip():
        return ToolResult(status="error", error="Не удалось извлечь текст страницы")
    return ToolResult(status="ok", payload={"url": final_url, "text": text[:20_000]})


async def _download_page(client: httpx.AsyncClient, initial_url: str) -> tuple[str, str]:
    current_url = initial_url
    for redirects in range(MAX_REDIRECTS + 1):
        async with client.stream("GET", current_url) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise httpx.HTTPStatusError(
                        "redirect response without Location",
                        request=response.request,
                        response=response,
                    )
                if redirects == MAX_REDIRECTS:
                    raise _TooManyRedirectsError
                current_url = urljoin(str(response.url), location)
                await asyncio.to_thread(validate_public_url, current_url)
                continue
            if response.status_code < 200 or response.status_code >= 300:
                response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("text/"):
                raise _UnsupportedContentTypeError
            body = await _read_limited(response.aiter_bytes())
            return str(response.url), body.decode(response.encoding or "utf-8", errors="replace")
    raise _TooManyRedirectsError


async def _read_limited(chunks: AsyncIterator[bytes]) -> bytes:
    body = bytearray()
    async for chunk in chunks:
        body.extend(chunk)
        if len(body) > MAX_PAGE_BYTES:
            raise _PageTooLargeError
    return bytes(body)


def _cached_rows(value: Any) -> list[dict[str, str]] | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return None
    try:
        rows = json.loads(value)
    except json.JSONDecodeError:
        return None
    return rows if _valid_rows(rows) else None


def _search_rows(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise ValueError("invalid SearXNG response")
    rows: list[dict[str, str]] = []
    for result in value["results"]:
        if not isinstance(result, dict):
            continue
        title: object = result.get("title")
        url: object = result.get("url")
        snippet: object = result.get("content")
        if isinstance(title, str) and isinstance(url, str) and isinstance(snippet, str):
            rows.append({"title": title, "url": url, "snippet": snippet})
        if len(rows) == 8:
            break
    return rows


def _valid_rows(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(row, dict)
        and set(row) == {"title", "url", "snippet"}
        and all(isinstance(item, str) for item in row.values())
        for row in value
    )


def _search_unavailable() -> ToolResult:
    return ToolResult(
        status="error",
        error="Поиск временно недоступен. Попробуйте позже.",
    )


def _as_web_search_args(args: BaseModel) -> WebSearchArgs:
    return WebSearchArgs.model_validate(args.model_dump())


def _as_fetch_page_args(args: BaseModel) -> FetchPageArgs:
    return FetchPageArgs.model_validate(args.model_dump())


class _PageTooLargeError(Exception):
    pass


class _UnsupportedContentTypeError(Exception):
    pass


class _TooManyRedirectsError(Exception):
    pass
