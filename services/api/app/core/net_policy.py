"""Outbound destination policy shared by every feature that lets a tenant supply a URL.

Webhook endpoints and Actions (HTTP calls the voice service makes during a call) are
URLs chosen by customers. Each one goes through this single check so neither ATLAS nor
the voice service can be pointed at internal infrastructure: https only (http only in
local development), no credentials embedded in the URL, and the host must resolve to
a public address. Callers re-check right before use because DNS answers change.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urlsplit

from app.core.config import Environment, get_settings
from app.core.exceptions import ValidationFailedError

Address = IPv4Address | IPv6Address
Resolver = Callable[[str, int], Awaitable[list[Address]]]


def is_blocked_address(ip: Address) -> bool:
    """True for anything that is not a public unicast address.

    Covers private, loopback, link-local (incl. cloud metadata 169.254.169.254),
    carrier-grade NAT, unspecified, reserved, documentation and multicast ranges, and
    IPv6 addresses that embed an IPv4 address (mapped, 6to4, Teredo).
    """
    if isinstance(ip, IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None:
            return is_blocked_address(embedded)
    return not ip.is_global or ip.is_multicast


async def resolve_host(host: str, port: int) -> list[Address]:
    """Resolve a hostname to every address it currently points at (monkeypatched in tests)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    addresses: list[Address] = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    return addresses


async def validate_destination_url(
    url: str,
    *,
    label: str,
    allow_private: bool,
    resolver: Resolver | None = None,
) -> str:
    """Enforce the destination policy and return the normalised (stripped) URL.

    `label` names the feature in error messages ("Webhook", "Action"). `allow_private`
    skips the address check for local development against services on the same machine;
    the scheme and credential checks always apply. `resolver` defaults to live DNS.
    """
    settings = get_settings()

    def reject(message: str) -> ValidationFailedError:
        return ValidationFailedError(message, details={"field": "url"})

    candidate = url.strip()
    try:
        parts = urlsplit(candidate)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise reject(f"{label} URL is not valid") from exc
    schemes = {"https"} | ({"http"} if settings.environment == Environment.LOCAL else set())
    if parts.scheme not in schemes:
        raise reject(f"{label} URL must use https")
    if not host or parts.username or parts.password:
        raise reject(f"{label} URL must have a host and must not embed credentials")
    if allow_private:
        return candidate
    try:
        literal: Address | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    resolve = resolver or resolve_host
    default_port = 80 if parts.scheme == "http" else 443
    addresses = [literal] if literal is not None else await resolve(host, port or default_port)
    if not addresses:
        raise reject(f"{label} host could not be resolved")
    if any(is_blocked_address(a) for a in addresses):
        raise reject(f"{label} URL must point to a public address")
    return candidate
