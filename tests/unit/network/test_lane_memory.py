# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the lane SERP-memory unwrapping (crawl affinity)."""

# pylint: disable=missing-module-docstring, protected-access

import asyncio
from unittest.mock import MagicMock

from searx.network.browser import (
    BrowserFetchError,
    BrowserFetchPool,
    _Lane,
    _crawl_request_blocked,
    _is_serp_wrap_url,
)

from tests import SearxTestCase

unwrap = BrowserFetchPool._unwrap_serp_href


class TestUnwrapSerpHref:
    def test_duckduckgo_wrapper(self):
        wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpage&rut=abc"
        assert unwrap(wrapped) == "https://example.org/page"

    def test_google_wrapper(self):
        wrapped = "https://www.google.com/url?q=https://example.org/a&sa=U&ved=2ahUKEwj"
        assert unwrap(wrapped) == "https://example.org/a"

    def test_bing_base64_wrapper(self):
        # u=a1<base64url("https://example.org/b")>
        wrapped = "https://www.bing.com/ck/a?!&u=a1aHR0cHM6Ly9leGFtcGxlLm9yZy9i&ntb=1"
        assert unwrap(wrapped) == "https://example.org/b"

    def test_direct_links_pass_through(self):
        assert unwrap("https://example.org/x") is None
        assert unwrap("https://search.brave.com/some/page") is None

    def test_garbage_is_safe(self):
        assert unwrap("https://www.bing.com/ck/a?u=a1%%%broken") is None
        assert unwrap("not a url at all") is None


class TestCanonicalSerpUrl:
    def test_google_goto_keeps_only_token(self):
        from searx.network.browser import _canonical_serp_url

        noisy = (
            "https://www.google.com/goto?url=CAESYwHr&sa=U&ved=2ahUKEwj&usg=AOvVaw"
        )
        assert _canonical_serp_url(noisy) == "https://www.google.com/goto?url=CAESYwHr"

    def test_plain_urls_pass_through(self):
        from searx.network.browser import _canonical_serp_url

        assert _canonical_serp_url("https://example.org/a?b=1") == "https://example.org/a?b=1"


class TestIsSerpWrapUrl:
    def test_google_goto(self):
        assert _is_serp_wrap_url("https://www.google.com/goto?url=CAESYwHr")

    def test_google_url_q(self):
        assert _is_serp_wrap_url("https://www.google.com/url?q=https://example.org/a&sa=U")

    def test_bing_ck(self):
        assert _is_serp_wrap_url("https://www.bing.com/ck/a?!&u=a1aHR0cHM6&ntb=1")

    def test_duckduckgo_l_uddg(self):
        assert _is_serp_wrap_url("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org")

    def test_publisher_host_is_never_a_wrapper(self):
        assert not _is_serp_wrap_url("https://example.org/goto?url=CAESYwHr")

    def test_search_page_itself_is_not_a_wrapper(self):
        assert not _is_serp_wrap_url("https://www.google.com/search?q=bangkok+news")

    def test_garbage(self):
        assert not _is_serp_wrap_url("not a url at all")


class TestCrawlRequestBlocked:
    def test_third_party_ad_hosts_blocked(self):
        assert _crawl_request_blocked("https://cdn.taboola.com/lib/trc.js")
        assert _crawl_request_blocked("https://connect.facebook.net/en_US/fbevents.js")
        assert _crawl_request_blocked("https://pixel.quantserve.com/pixel.js")

    def test_google_ad_pipeline_allowed_everywhere(self):
        # googlesyndication/doubleclick/GA/GTM are the cross-site jar
        # signal: allowed on crawled pages too
        assert not _crawl_request_blocked("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js")
        assert not _crawl_request_blocked("https://googleads.g.doubleclick.net/pagead/ads")
        assert not _crawl_request_blocked("https://www.google-analytics.com/analytics.js")
        assert not _crawl_request_blocked("https://www.googletagmanager.com/gtm.js?id=x")

    def test_publisher_and_fonts_allowed(self):
        assert not _crawl_request_blocked("https://example.org/page")
        assert not _crawl_request_blocked("https://fonts.googleapis.com/css2?family=x")
        assert not _crawl_request_blocked("https://www.google.com/search?q=x")

    def test_video_media_blocked(self):
        assert _crawl_request_blocked("https://cdn.example.org/clip.mp4?token=1")
        assert _crawl_request_blocked("https://stream.example.org/hls/index.m3u8")

    def test_garbage_is_allowed(self):
        assert not _crawl_request_blocked("not a url at all")


class TestCrossLaneWrapperGuard(SearxTestCase):
    """A wrapper link whose minting lane is not in memory must be refused,
    not crawled from whichever lane happens to be free."""

    @staticmethod
    def _pool_with_free_lane(lane):
        pool = BrowserFetchPool(post_search_browsing=False)
        pool._lanes = [lane]
        pool._lane_cycle = asyncio.Queue()
        pool._lane_cycle.put_nowait(lane)

        async def _noop(*_args, **_kwargs):
            return None

        pool._init = _noop
        pool._ensure_browser_alive = _noop
        return pool

    async def test_wrapper_without_memory_is_refused(self):
        lane = _Lane(None, None, ":91")
        pool = self._pool_with_free_lane(lane)
        with self.assertRaises(BrowserFetchError):
            await pool._checkout_for_crawl("https://www.google.com/goto?url=CAESYwHr")
        assert pool._lane_cycle.qsize() == 1  # the free lane was not consumed

    async def test_publisher_url_still_claims_free_lane(self):
        lane = _Lane(None, None, ":92")
        pool = self._pool_with_free_lane(lane)
        got, affinity, borrowed = await pool._checkout_for_crawl("https://example.org/page")
        assert got is lane
        assert not affinity and not borrowed

    async def test_wrapper_with_minting_lane_memory_rides_affinity(self):
        lane = _Lane(None, MagicMock(), ":93")
        lane.context.is_closed.return_value = False  # alive
        lane.busy = True  # mid-request: the crawl rides its own jar
        pool = BrowserFetchPool(post_search_browsing=False)
        pool._lanes = [lane]
        lane.serp_urls.append("https://www.google.com/goto?url=CAESYwHr")

        async def _noop(*_args, **_kwargs):
            return None

        pool._init = _noop
        pool._ensure_browser_alive = _noop
        got, affinity, borrowed = await pool._checkout_for_crawl(
            "https://www.google.com/goto?url=CAESYwHr"
        )
        assert got is lane
        assert affinity and borrowed
