# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name

import mock

from searx.engines import google, google_images
from searx.extended_types import SXNG_URL
from tests import SearxTestCase


class _StubTraits:
    """Minimal EngineTraits stand-in so google_request can be called
    without the engine having been initialized."""

    all_locale = "ZZ"

    def get_language(self, sxng_locale, default=None):
        return "lang_en"

    def get_region(self, sxng_locale, default=None):
        return "US"


def _params():
    return {"headers": {}, "cookies": {}, "pageno": 1, "searxng_locale": "en-US", "time_range": None, "safesearch": 0}


class TestGoogleImagesRequest(SearxTestCase):
    def test_request_desktop_interface_when_browser_pool_serves(self):
        params = _params()
        with mock.patch.object(google, "_browser_serves_requests", return_value=True):
            with mock.patch.object(google_images, "traits", _StubTraits(), create=True):
                google_images.request("apple", params)
        self.assertTrue(params["url"].startswith("https://www.google.com/search?"))
        self.assertIn("q=apple", params["url"])
        self.assertIn("tbm=isch", params["url"])
        # the pool's stable desktop identity signs the request: no engine
        # user agent, no impersonation target
        self.assertNotIn("User-Agent", params["headers"])
        self.assertNotIn("impersonate", params)

    def test_request_legacy_wml_without_browser_pool(self):
        params = _params()
        with mock.patch.object(google, "_browser_serves_requests", return_value=False):
            with mock.patch.object(google_images, "traits", _StubTraits(), create=True):
                google_images.request("apple", params)
        self.assertTrue(params["url"].startswith("https://www.google.com/wml/search?"))
        self.assertIn("tbm=isch", params["url"])
        self.assertIn("User-Agent", params["headers"])
        self.assertEqual(params["impersonate"], "chrome99_android")

    def test_no_identity_header_override(self):
        # the WML-era flag kept the Nokia UA on the browser pool's jar;
        # the desktop interface must not reintroduce it
        self.assertFalse(hasattr(google_images, "browser_keep_identity_headers"))


class TestGoogleImagesDesktopResponse(SearxTestCase):
    """response() on the desktop image grid (masqueraded browser pool)."""

    desktop_html = """
    <html><body><div id="main"><div id="cnt">
      <div id="search">
        <div class="offbPb ivg-i" data-lpage="https://example.com/apple">
          <a><div><img alt="A red apple on a table"
            src="https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcT1&amp;s=10"></div></a>
          <a href="/goto?url=CAES"><div>A red apple on a table</div></a>
          <a href="/imgres?imgurl=https%3A%2F%2Fexample.com%2Fapple_full.jpg&amp;imgrefurl=https%3A%2F%2Fexample.com%2Fapple&amp;w=800&amp;h=600&amp;tbnid=ANd9GcT1"></a>
        </div>
        <div class="offbPb ivg-i" data-lpage="https://example.com/pears">
          <a><div><img alt="Green pears in a bowl"
            src="data:image/jpeg;base64,AAAA"></div></a>
          <a href="/goto?url=CAFR"><div>Green pears in a bowl</div></a>
        </div>
        <a href="/imgres?imgurl=https%3A%2F%2Fcdn.example.net%2Fcherry.png&amp;imgrefurl=https%3A%2F%2Fexample.net%2Fcherry&amp;w=64&amp;h=64&amp;tbnid=ANd9GcCH"></a>
      </div>
    </div></div></body></html>
    """

    def _resp(self, html: str, url: str = "https://www.google.com/search?q=test&tbm=isch&udm=2"):
        resp = mock.MagicMock()
        resp.text = html
        resp.url = SXNG_URL(url)
        resp.status_code = 200
        return resp

    def test_response_parses_desktop_grid(self):
        results = list(google_images.response(self._resp(self.desktop_html)))
        self.assertEqual(len(results), 3)

        first = results[0]
        self.assertEqual(first.url, "https://example.com/apple")
        self.assertEqual(first.title, "A red apple on a table")
        # the tile's legacy /imgres link carries the full-resolution image
        self.assertEqual(first.img_src, "https://example.com/apple_full.jpg")
        self.assertEqual(
            first.thumbnail_src,
            "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcT1&s=10",
        )
        self.assertEqual(first.resolution, "800 x 600")

        # an unpainted tile (inline placeholder, no /imgres link) still
        # yields the source page and the title
        second = results[1]
        self.assertEqual(second.url, "https://example.com/pears")
        self.assertEqual(second.title, "Green pears in a bowl")
        self.assertEqual(second.img_src, "")

        # a bare /imgres link outside the tiles (classic grid)
        third = results[2]
        self.assertEqual(third.url, "https://example.net/cherry")
        self.assertEqual(third.img_src, "https://cdn.example.net/cherry.png")
        self.assertEqual(third.resolution, "64 x 64")
        self.assertEqual(third.thumbnail_src, "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcCH")

    def test_response_deduplicates_tile_and_imgres(self):
        # the /imgres link inside the apple tile is already covered by the
        # tile itself: no duplicate (url, img_src) pair
        results = list(google_images.response(self._resp(self.desktop_html)))
        pairs = [(r.url, r.img_src) for r in results]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_response_on_web_serp_yields_nothing(self):
        web_serp = """
        <html><body><div id="search"><div id="rso">
          <div><a href="https://example.com/x"><h3>Example</h3></a></div>
        </div></div></body></html>
        """
        results = list(google_images.response(self._resp(web_serp)))
        self.assertEqual(results, [])


class TestGoogleImagesWmlResponse(SearxTestCase):
    """The legacy Nokia/WML layout keeps parsing (curl mode)."""

    wml_html = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<document>"
        '<a href="/imgres?imgurl=https%3A%2F%2Fcdn.example.net%2Fx.jpg&amp;imgrefurl=https%3A%2F%2Fexample.org%2Fx'
        '&amp;w=32&amp;h=32&amp;tbnid=ANd9GcX">x</a>'
        "</document>"
    )

    def _resp(self, html: str, url: str = "https://www.google.com/wml/search?q=test&tbm=isch"):
        resp = mock.MagicMock()
        resp.text = html
        resp.content = html.encode()
        resp.url = SXNG_URL(url)
        resp.status_code = 200
        return resp

    def test_response_parses_wml_layout(self):
        results = list(google_images.response(self._resp(self.wml_html)))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.org/x")
        self.assertEqual(results[0].img_src, "https://cdn.example.net/x.jpg")
        self.assertEqual(results[0].thumbnail_src, "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcX")
        self.assertEqual(results[0].resolution, "32 x 32")

    def test_response_raises_on_sorry_page(self):
        from searx.exceptions import SearxEngineCaptchaException

        resp = self._resp("unusual traffic", url="https://www.google.com/sorry/index?continue=x")
        with self.assertRaises(SearxEngineCaptchaException):
            google_images.response(resp)
