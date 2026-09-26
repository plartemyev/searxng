"""Page crawling on the masqueraded browser pool.

The /crawl endpoint (searx/webapp.py) lets other components of this
deployment reuse the masqueraded Chromium lanes for page fetching: one
browser fleet serves search and crawl traffic, so every visit comes from
the same cookie-trained, fingerprint-consistent identity. The endpoint is
off by default (``outgoing.browser_crawl_endpoint``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import socket
from urllib.parse import urlsplit

from searx.network.browser import (
    BrowserFetchError,
    CrawlBodyTooLarge,
    get_browser_fetch_pool,
    unwrap_google_translate_url,
)
from searx.network.client import get_loop

__all__ = [
    "BrowserFetchError",
    "CrawlBodyTooLarge",
    "CrawlError",
    "fetch_bytes",
    "render_page",
]


class CrawlError(BrowserFetchError):
    """Rejected crawl request (bad URL, private network, ...)."""


def _assert_crawlable_url(url: str, allow_private_network: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CrawlError("url must be an absolute http(s) URL")
    if allow_private_network:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror as err:
        raise CrawlError(f"cannot resolve {parts.hostname}") from err
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise CrawlError("refusing to crawl a private-network address")


def render_page(
    url: str, *, timeout_s: float = 30.0, allow_private_network: bool = False
) -> dict:
    """Render one page on a lane browser: html + final URL (redirects
    followed, Google Translate wrappers resolved to the original)."""
    url = unwrap_google_translate_url(url)
    _assert_crawlable_url(url, allow_private_network)
    future = asyncio.run_coroutine_threadsafe(
        get_browser_fetch_pool().crawl_render(url, timeout_s=timeout_s), get_loop()
    )
    try:
        return future.result(timeout_s + 90.0)
    except concurrent.futures.TimeoutError as err:
        raise CrawlError("crawl timed out waiting for a browser lane") from err


def fetch_bytes(
    url: str,
    *,
    timeout_s: float = 30.0,
    max_bytes: int = 52428800,
    allow_private_network: bool = False,
) -> dict:
    """Fetch raw bytes in a lane's browser context (browser TLS + cookies)."""
    url = unwrap_google_translate_url(url)
    _assert_crawlable_url(url, allow_private_network)
    future = asyncio.run_coroutine_threadsafe(
        get_browser_fetch_pool().crawl_bytes(
            url, timeout_s=timeout_s, max_bytes=max_bytes
        ),
        get_loop(),
    )
    try:
        return future.result(timeout_s + 90.0)
    except concurrent.futures.TimeoutError as err:
        raise CrawlError("crawl timed out waiting for a browser lane") from err
