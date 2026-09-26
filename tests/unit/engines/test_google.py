# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name

from urllib.parse import urlsplit

import mock

from searx.engines import google
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


class TestGoogleRequest(SearxTestCase):
    def test_request_legacy_layout(self):
        params = {"headers": {}, "cookies": {}, "pageno": 1, "searxng_locale": "en-US", "time_range": None, "safesearch": 0}
        google.google_request("weather", params, eng_traits=_StubTraits())  # pylint: disable=protected-access
        self.assertTrue(params["url"].startswith("https://www.google.com/wml/search?"))
        self.assertIn("User-Agent", params["headers"])
        self.assertEqual(params["impersonate"], "chrome99_android")

    def test_request_desktop_interface_when_browser_pool_serves(self):
        params = {"headers": {}, "cookies": {}, "pageno": 1, "searxng_locale": "en-US", "time_range": None, "safesearch": 0}
        with mock.patch.object(google, "_browser_serves_requests", return_value=True):
            google.google_request(  # pylint: disable=protected-access
                "weather", params, eng_traits=_StubTraits(), desktop_interface=True
            )
        url, query = params["url"].split("?", 1)
        self.assertEqual(url, "https://www.google.com/search")
        self.assertIn("q=weather", query)
        self.assertNotIn("User-Agent", params["headers"])
        self.assertNotIn("impersonate", params)


class TestGoogleDesktopResponse(SearxTestCase):
    """response() on the desktop HTML layout (masqueraded browser pool)."""

    desktop_html = """
    <html><body><div id="main"><div id="cnt">
      <div id="topstuff"></div>
      <div id="search">
        <div id="rso">
          <div class="MjjYud">
            <div class="g Ww4FFb vt6azd">
              <div>
                <a href="https://example.com/searxng">
                  <h3 class="LC20lb MBeuO DKV0Md">SearXNG - a privacy-respecting metasearch engine</h3>
                </a>
                <div class="VwiC3b yXK7l MUxGbd yDYNvb lyLwlc">
                  SearXNG is a free internet metasearch engine which aggregates
                  results from up to 242 search services.
                </div>
              </div>
            </div>
          </div>
          <div class="MjjYud">
            <div class="g">
              <div>
                <a href="/url?q=https://docs.searxng.org/&amp;sa=U&amp;ved=2ahUKEwj">
                  <h3>Documentation of SearXNG</h3>
                </a>
                <div class="VwiC3b">The documentation covers administration and development.</div>
              </div>
            </div>
          </div>
          <div class="MjjYud">
            <div class="g">
              <div>
                <a href="https://www.google.com/search?q=searxng+instances">
                  <h3>searxng instances</h3>
                </a>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div id="botstuff">
        <a class="k8XOCe" href="/search?q=searxng+docker"><div class="s75CSd">searxng docker</div></a>
      </div>
    </div></div></body></html>
    """

    def _resp(self, html: str, url: str = "https://www.google.com/search?q=test"):
        resp = mock.MagicMock()
        resp.text = html
        resp.url = SXNG_URL(url)
        resp.status_code = 200
        return resp

    def test_response_parses_desktop_layout(self):
        resp = self._resp(self.desktop_html)
        results = list(google.response(resp))

        main = [r for r in results if getattr(r, "url", None)]
        self.assertEqual(len(main), 2)
        self.assertEqual(main[0].url, "https://example.com/searxng")
        self.assertEqual(main[0].title, "SearXNG - a privacy-respecting metasearch engine")
        self.assertIn("aggregates", main[0].content)
        # /url?q= redirector unwrapped
        self.assertEqual(main[1].url, "https://docs.searxng.org/")

        suggestions = [getattr(r, "suggestion", None) for r in results]
        self.assertIn("searxng docker", suggestions)

    def test_response_keeps_encrypted_goto_links(self):
        html = """
        <html><body><div id="rso">
          <div class="g">
            <a href="/goto?url=CAESgwEB6zswFZ2Jd9EVFTvydtJ4FRrW427FWj8"><h3>How Do Solar Panels Work?</h3></a>
            <div class="VwiC3b">Sunlight hits the panel and the inverter converts the current.</div>
          </div>
        </div></body></html>
        """
        resp = self._resp(html)
        results = list(google.response(resp))

        main = [r for r in results if getattr(r, "url", None)]
        self.assertEqual(len(main), 1)
        self.assertEqual(
            main[0].url,
            "https://www.google.com/goto?url=CAESgwEB6zswFZ2Jd9EVFTvydtJ4FRrW427FWj8",
        )
        self.assertEqual(main[0].title, "How Do Solar Panels Work?")
        self.assertIn("Sunlight hits the panel", main[0].content)

    def test_response_falls_back_to_wml_layout(self):
        resp = self._resp(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<document><div class="zMzFAb"></div></document>',
            url="https://www.google.com/wml/search?q=test",
        )
        results = list(google.response(resp))
        self.assertEqual(results, [])

    def test_response_raises_on_sorry_page(self):
        from searx.exceptions import SearxEngineCaptchaException

        resp = self._resp("unusual traffic", url="https://www.google.com/sorry/index?continue=x")
        with self.assertRaises(SearxEngineCaptchaException):
            google.response(resp)


class TestGoogleWmlResponse(SearxTestCase):
    """The legacy Nokia/WML layout keeps parsing (curl mode)."""

    wml_html = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<document><div>"
        '<div class="zMzFAb">'
        '<a class="fuLhoc" href="https://example.com/one">'
        '<span class="CVA68e">Example One</span></a>'
        '<div class="taTFJ"><span class="FrIlee">first example</span></div>'
        "</div>"
        '<table class="HExoMb"><a class="ZWRArf">other query</a></table>'
        "</div></document>"
    )

    def _resp(self, html: str, url: str = "https://www.google.com/wml/search?q=test"):
        resp = mock.MagicMock()
        resp.text = html
        resp.content = html.encode()
        resp.url = SXNG_URL(url)
        resp.status_code = 200
        return resp

    def test_response_parses_wml_layout(self):
        resp = self._resp(self.wml_html)
        results = list(google.response(resp))
        main = [r for r in results if getattr(r, "url", None)]
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0].url, "https://example.com/one")
        self.assertEqual(main[0].title, "Example One")

    def test_suggestion_xpath_still_wired(self):
        self.assertIn("HExoMb", google.suggestion_xpath)


class TestUnwrapGoogleUrl(SearxTestCase):
    def test_unwrap(self):
        self.assertEqual(
            google.unwrap_google_url("/url?q=https://example.com/a&sa=U&ved=2"),
            "https://example.com/a",
        )
        self.assertEqual(google.unwrap_google_url("https://example.com/a"), "https://example.com/a")
        # the encrypted redirect is kept: it leads to the target when
        # followed, and decoding it inorganically would trip antibot
        self.assertEqual(
            google.unwrap_google_url("/goto?url=CAESgwEB6zswFZ2Jd9EVFTvydtJ4FRrW427FWj8"),
            "https://www.google.com/goto?url=CAESgwEB6zswFZ2Jd9EVFTvydtJ4FRrW427FWj8",
        )
