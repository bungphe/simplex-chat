"""Fetching customer attachments (photos, files) for the admin inbox.

The page's Content-Security-Policy only allows images from this server, so remote
attachments are fetched here and passed through. Only URLs already stored with a
message are fetched (never a URL from the request), only from public addresses, with
a size cap; anything that is not a plain raster image is served as a download, so an
uploaded HTML or SVG file can never run in the admin page's origin.

The host is resolved once and the request goes to the address that was checked (the
name is kept for the Host header and TLS), so a DNS answer that changes between the
check and the connection (DNS rebinding) cannot point the request at the intranet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2

MAX_BYTES = 15 * 1024 * 1024
INLINE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ai-employees-inbox/1.0)", "Accept": "*/*"}


class MediaError(Exception):
    pass


async def resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(i[4][0]) for i in infos]


async def check_public(url: str) -> list[str]:
    """The host's addresses, all public (or MediaError)."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise MediaError("only http(s) URLs")
    try:
        addresses = await resolve(parts.hostname)
    except OSError as e:
        raise MediaError(f"cannot resolve {parts.hostname}") from e
    if not addresses:
        raise MediaError(f"cannot resolve {parts.hostname}")
    for a in addresses:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise MediaError("address is not public")
    return [a.split("%", 1)[0] for a in addresses]


def _proxied(http: httpx2.AsyncClient, u: httpx2.URL) -> bool:
    """Whether the client sends this URL through a proxy (HTTPS_PROXY...): the proxy then
    resolves the name itself, and many refuse a bare address."""
    pick = getattr(http, "_transport_for_url", None)
    return pick is not None and pick(u) is not getattr(http, "_transport", None)


async def pinned(http: httpx2.AsyncClient, url: str) -> tuple[httpx2.URL, dict[str, str], dict[str, Any]]:
    """The request for `url` sent to the checked address: (URL, headers, extensions)."""
    addresses = await check_public(url)
    try:
        u = httpx2.URL(url)
    except (httpx2.InvalidURL, ValueError) as e:
        raise MediaError("invalid URL") from e
    if _proxied(http, u):
        return u, dict(HEADERS), {}
    host = u.netloc.decode("ascii")
    # a fresh connection per file: a pooled one may have been opened for another name
    headers = {**HEADERS, "Host": host, "Connection": "close"}
    extensions = {"sni_hostname": u.raw_host.decode("ascii")} if u.scheme == "https" else {}
    return u.copy_with(host=addresses[0]), headers, extensions


async def read_limited(r: httpx2.Response, limit: int | None = None) -> bytes:
    """A streamed response's body, or MediaError when it is bigger than `limit` (MAX_BYTES)."""
    limit = MAX_BYTES if limit is None else limit
    if int(r.headers.get("content-length") or 0) > limit:
        raise MediaError("file too large")
    body = bytearray()
    async for chunk in r.aiter_bytes():
        body += chunk
        if len(body) > limit:
            raise MediaError("file too large")
    return bytes(body)


async def fetch(http: httpx2.AsyncClient, url: str) -> tuple[str, bytes]:
    """GET a public URL (following up to 3 redirects, each checked); returns (type, body)."""
    for _ in range(4):
        target, headers, extensions = await pinned(http, url)
        request = http.build_request("GET", target, headers=headers, timeout=20.0, extensions=extensions)
        r = await http.send(request, stream=True, follow_redirects=False)
        try:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                raise MediaError(f"HTTP {r.status_code}")
            body = await read_limited(r)
            ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            return ctype, body
        finally:
            await r.aclose()
    raise MediaError("too many redirects")
