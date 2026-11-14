"""Requests to URLs that people or remote servers give OpenRing: never to cloud metadata or link-local addresses.

Tool servers, `server.url`, `custom-llm` base URLs and the import source come from assistants, phone numbers
and API calls. On a cloud server, a URL such as http://169.254.169.254/ would let whoever sets one read the
machine's cloud credentials through OpenRing (a tool's answer lands in the call's transcript; the import
report repeats what the source sent). Those addresses are refused. Private and local networks stay allowed:
a tool server on the office network, or a model on this computer, is a normal setup.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import threading
from typing import Any
from urllib.parse import urlsplit

import httpx

_TLS: ssl.SSLContext | None = None
_TLS_LOCK = threading.Lock()


def tls() -> ssl.SSLContext:
    """The TLS context of every request OpenRing makes. Building one reads the whole CA bundle: about 0.2 s on a
    laptop, during which the event loop, and every live call on it, stands still (a real call lost words to it: each
    server message built a new client). So it is built once, at startup (`warm_up`), and shared."""
    global _TLS
    if _TLS is None:
        with _TLS_LOCK:
            if _TLS is None:
                _TLS = httpx.create_ssl_context()
    return _TLS


async def warm_up() -> None:
    """Build the TLS context away from the event loop, before the first call needs it."""
    await asyncio.to_thread(tls)


def http_client(**options: Any) -> httpx.AsyncClient:
    """An httpx client for OpenRing's own requests (a provider's API), sharing the TLS context."""
    options.setdefault("verify", tls())
    return httpx.AsyncClient(**options)

BLOCKED = [ipaddress.ip_network(n) for n in (
    "169.254.0.0/16",      # link-local IPv4: AWS, GCP, Azure, DigitalOcean, Oracle metadata (169.254.169.254)
    "fe80::/10",           # link-local IPv6
    "fd00:ec2::254/128",   # AWS metadata over IPv6
    "100.100.100.200/32",  # Alibaba Cloud metadata
    "0.0.0.0/8",           # "this network": reaches the machine itself in surprising ways
    "224.0.0.0/4",         # multicast
    "ff00::/8",            # IPv6 multicast
)]


class UnsafeURL(ValueError):
    """A URL OpenRing refuses to call."""


def blocked_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone ("fe80::1%eth0")
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:  # ::ffff:169.254.169.254
        ip = ip.ipv4_mapped
    return any(ip in net for net in BLOCKED if ip.version == net.version)


def _host(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"only http and https URLs are allowed, not '{parts.scheme or url}'")
    if not parts.hostname:
        raise UnsafeURL(f"'{url}' has no host")
    return parts.hostname


def _refuse(host: str, address: str) -> None:
    if blocked_address(address):
        raise UnsafeURL(f"{host} points to {address}, a cloud metadata or link-local address OpenRing never calls")


def check_url(url: str) -> None:
    """Refuse a URL whose host is, or resolves to, a blocked address. A host that doesn't resolve passes:
    the request itself then fails with the usual error."""
    host = _host(url)
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return
    for info in infos:
        _refuse(host, str(info[4][0]))


async def check_url_async(url: str) -> None:
    host = _host(url)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return
    for info in infos:
        _refuse(host, str(info[4][0]))


class GuardedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that checks every request's host first, redirects included."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None):
        self._inner = inner or httpx.AsyncHTTPTransport(verify=tls())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            await check_url_async(str(request.url))
        except UnsafeURL as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def client(**options: Any) -> httpx.AsyncClient:
    """An httpx client for URLs that come from outside: it never calls a blocked address."""
    options.setdefault("verify", tls())  # used by the proxies an environment sets, if any
    return httpx.AsyncClient(transport=GuardedTransport(), **options)


def safe_url_for_logs(url: str) -> str:
    """Scheme and host only: webhook URLs often carry a secret in their path or query (Make, Zapier, n8n)."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "(invalid URL)"
    if not parts.scheme or not parts.hostname:
        return "(invalid URL)"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}/…" if (parts.path not in ("", "/") or parts.query) \
        else f"{parts.scheme}://{parts.hostname}{port}"
