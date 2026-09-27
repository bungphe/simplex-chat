"""Fetching customer attachments (photos, files) for the admin inbox.

The page's Content-Security-Policy only allows images from this server, so remote
attachments are fetched here and passed through. Only URLs already stored with a
message are fetched (never a URL from the request), only from public addresses, with
a size cap; anything that is not a plain raster image is served as a download, so an
uploaded HTML or SVG file can never run in the admin page's origin.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
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


async def check_public(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise MediaError("only http(s) URLs")
    try:
        addresses = await resolve(parts.hostname)
    except OSError as e:
        raise MediaError(f"cannot resolve {parts.hostname}") from e
    for a in addresses:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise MediaError("address is not public")


async def fetch(http: httpx2.AsyncClient, url: str) -> tuple[str, bytes]:
    """GET a public URL (following up to 3 redirects, each checked); returns (type, body)."""
    for _ in range(4):
        await check_public(url)
        async with http.stream("GET", url, headers=HEADERS, timeout=20.0, follow_redirects=False) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                raise MediaError(f"HTTP {r.status_code}")
            if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                raise MediaError("file too large")
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise MediaError("file too large")
            ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            return ctype, bytes(body)
    raise MediaError("too many redirects")
