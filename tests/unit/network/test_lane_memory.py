# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the lane SERP-memory unwrapping (crawl affinity)."""

# pylint: disable=missing-module-docstring, protected-access

from searx.network.browser import BrowserFetchPool

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
