"""Offline tests for the outbound URL SSRF boundary."""

from __future__ import annotations

import socket

import pytest

from core.ssrf import SsrfBlockedError, validate_public_url


def _resolver(*addresses: str):
    def resolve(_host: str, _port: int | None) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
    ],
)
def test_literal_private_addresses_are_rejected(url: str) -> None:
    with pytest.raises(SsrfBlockedError):
        validate_public_url(url)


def test_localhost_and_mixed_dns_results_are_rejected() -> None:
    with pytest.raises(SsrfBlockedError):
        validate_public_url("https://localhost/", resolver=_resolver("127.0.0.1"))
    with pytest.raises(SsrfBlockedError):
        validate_public_url(
            "https://mixed.example/", resolver=_resolver("93.184.216.34", "10.0.0.1")
        )


def test_public_literal_and_dns_result_are_allowed() -> None:
    validate_public_url("https://93.184.216.34/")
    validate_public_url("https://public.example/", resolver=_resolver("93.184.216.34"))


@pytest.mark.parametrize("url", ["ftp://public.example/", "file:///tmp/page", "not a url"])
def test_non_http_urls_are_rejected(url: str) -> None:
    with pytest.raises(SsrfBlockedError):
        validate_public_url(url, resolver=_resolver("93.184.216.34"))
