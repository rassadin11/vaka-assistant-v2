"""Pure URL validation helpers for outbound web requests."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

type AddressInfo = tuple[object, ...]
type Resolver = Callable[..., Sequence[AddressInfo]]
type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class SsrfBlockedError(ValueError):
    """Raised when a URL may reach a non-public network address."""


def validate_public_url(url: str, resolver: Resolver = socket.getaddrinfo) -> None:
    """Ensure an HTTP(S) URL resolves exclusively to globally routable addresses."""

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SsrfBlockedError("invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SsrfBlockedError("only public HTTP(S) URLs are allowed")

    hostname = parsed.hostname
    try:
        addresses = (
            [_literal_ip(hostname)] if _is_literal_ip(hostname) else _resolve(hostname, resolver)
        )
    except (OSError, ValueError) as exc:
        raise SsrfBlockedError("host could not be resolved") from exc

    if not addresses or any(_is_blocked(address) for address in addresses):
        raise SsrfBlockedError("host resolves to a non-public address")


def _resolve(hostname: str, resolver: Resolver) -> list[IPAddress]:
    resolved = resolver(hostname, None)
    addresses: list[IPAddress] = []
    for item in resolved:
        if len(item) < 5 or not isinstance(item[4], tuple) or not item[4]:
            raise ValueError("invalid resolver response")
        address = item[4][0]
        if not isinstance(address, str):
            raise ValueError("invalid resolved address")
        addresses.append(ipaddress.ip_address(address))
    return addresses


def _is_literal_ip(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _literal_ip(hostname: str) -> IPAddress:
    return ipaddress.ip_address(hostname)


def _is_blocked(address: IPAddress) -> bool:
    return (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )
