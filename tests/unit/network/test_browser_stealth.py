# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the max-stealth browser routing helpers."""

# pylint: disable=missing-module-docstring

import asyncio
import base64
import json
import os
import time
from types import SimpleNamespace

import pytest

from searx.network import browser as browser_module
from searx.network import human_input as human_input_module
from searx.network.browser import (
    _geo_cache_read,
    _geo_cache_write,
    _homepage_url_from_search_url,
    _ip_locale_from_payload,
    _is_api_url,
    _is_human_search_candidate,
)


# _is_api_url: data endpoints keep the fetch path, search UIs do not

@pytest.mark.parametrize(
    'url',
    [
        'https://www.google.com/search?q=test',
        'https://duckduckgo.com/?q=test&iar=images',
        'https://search.brave.com/search?q=test',
        'https://en.wikipedia.org/w/index.php?search=test',
    ],
)
def test_is_api_url_false_for_search_uis(url):
    assert _is_api_url(url) is False


@pytest.mark.parametrize(
    'url',
    [
        'https://duckduckgo.com/i.js?q=test&vqd=4',
        'https://www.bing.com/images/async?q=test&first=0',
        'https://www.google.com/complete/search?q=te&client=firefox',
        'https://duckduckgo.com/ac/?q=te&type=list',
        'https://api.exmaple.org/search?q=test',
        'https://example.org/data.json?q=test',
    ],
)
def test_is_api_url_true_for_data_endpoints(url):
    assert _is_api_url(url) is True


# _is_human_search_candidate: who gets the interactive visit


@pytest.mark.parametrize(
    'url',
    [
        'https://www.google.com/search?q=test',
        'https://search.brave.com/search?q=test',
        'https://en.wikipedia.org/w/index.php?search=test',
    ],
)
def test_is_human_search_candidate_true(url):
    assert _is_human_search_candidate(url) is True


@pytest.mark.parametrize(
    'url',
    [
        # duckduckgo.com is exempt: not bot-walled, and its image engine
        # bootstraps a vqd token from the front page within a short timeout
        'https://duckduckgo.com/?q=test&iar=images&t=h_',
        'https://duckduckgo.com/i.js?q=test&vqd=4',
        'https://www.bing.com/images/async?q=test&first=0',
    ],
)
def test_is_human_search_candidate_false(url):
    assert _is_human_search_candidate(url) is False


# _homepage_url_from_search_url: the human flow starts from the front page,
# carrying the engine URL's locale so the provider renders the requested
# language and its own form submits it with the typed query


def test_homepage_carries_hl_and_cr_as_gl():
    url = (
        'https://www.google.com/search?q=garten'
        '&hl=de&lr=lang_de&cr=countryDE&ie=utf8&oe=utf8&sca_esv=1'
    )
    assert _homepage_url_from_search_url(url) == 'https://www.google.com/?hl=de&gl=DE'


def test_homepage_prefers_explicit_gl():
    url = 'https://www.google.com/search?q=test&hl=de&gl=AT&cr=countryDE'
    assert _homepage_url_from_search_url(url) == 'https://www.google.com/?hl=de&gl=AT'


def test_homepage_without_locale_params_is_bare():
    assert (
        _homepage_url_from_search_url('https://www.bing.com/search?q=test&form=QBLH')
        == 'https://www.bing.com/'
    )


def test_homepage_keeps_host_and_scheme():
    url = 'https://search.brave.com/search?q=test&hl=de'
    assert _homepage_url_from_search_url(url) == 'https://search.brave.com/?hl=de'


def test_homepage_carries_bing_locale_params():
    url = 'https://www.bing.com/search?q=test&mkt=de-DE&setlang=de&form=QBLH'
    assert (
        _homepage_url_from_search_url(url)
        == 'https://www.bing.com/?mkt=de-DE&setlang=de'
    )


# _ip_locale_from_payload: the lane identity must match the public IP


def test_ip_locale_from_geojs_payload():
    locale = _ip_locale_from_payload(
        {
            'country_code': 'TH',
            'timezone': 'Asia/Bangkok',
            'latitude': '13.7563',
            'longitude': '100.5018',
        }
    )
    assert locale['locale'] == 'th-TH'
    assert locale['timezone'] == 'Asia/Bangkok'
    assert locale['geolocation'] == {
        'latitude': 13.7563,
        'longitude': 100.5018,
        'accuracy': 40.0,
    }


def test_ip_locale_from_ipwho_payload():
    locale = _ip_locale_from_payload(
        {
            'country_code': 'DE',
            'timezone': {'id': 'Europe/Berlin', 'abbr': '+02'},
            'latitude': 52.52,
            'longitude': 13.4,
        }
    )
    assert locale['locale'] == 'de-DE'
    assert locale['timezone'] == 'Europe/Berlin'
    assert locale['accept_language'] == 'de-DE,en;q=0.9'


def test_ip_locale_from_ipinfo_payload():
    locale = _ip_locale_from_payload(
        {'country': 'JP', 'timezone': 'Asia/Tokyo', 'loc': '35.68,139.69'}
    )
    assert locale['locale'] == 'ja-JP'
    assert locale['timezone'] == 'Asia/Tokyo'
    assert locale['geolocation'] == {
        'latitude': 35.68,
        'longitude': 139.69,
        'accuracy': 40.0,
    }
    assert 'en' in locale['accept_language']


def test_ip_locale_languages_field_wins_over_table():
    # a provider that carries languages keeps the multi-code accept-language
    locale = _ip_locale_from_payload(
        {'country_code': 'DE', 'languages': 'de,de-DE', 'timezone': 'Europe/Berlin'}
    )
    assert locale['locale'] == 'de-DE'
    assert locale['accept_language'] == 'de-DE,de;q=0.9,en;q=0.7'


def test_ip_locale_unknown_country_stays_english():
    locale = _ip_locale_from_payload({'country_code': 'XK', 'timezone': 'Europe/Podgorica'})
    assert locale['locale'] == 'en-US'
    assert locale['timezone'] == 'Europe/Podgorica'


def test_ip_locale_survives_missing_coordinates():
    locale = _ip_locale_from_payload(
        {'country_code': 'FR', 'timezone': 'Europe/Paris'}
    )
    assert locale['geolocation'] is None
    assert locale['locale'] == 'fr-FR'
    assert locale['timezone'] == 'Europe/Paris'


def test_ip_locale_requires_a_country():
    assert _ip_locale_from_payload({'timezone': 'Europe/Berlin'}) is None


# _geo_cache_read / _geo_cache_write: the identity survives restarts


def test_geo_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, '_GEO_CACHE_PATH', str(tmp_path / 'ip_locale.json'))
    _geo_cache_write(
        {'country_code': 'TH', 'timezone': 'Asia/Bangkok', 'latitude': 13.7, 'longitude': 100.5}
    )
    locale = _geo_cache_read()
    assert locale['locale'] == 'th-TH'
    assert 'cache' in locale['source']


def test_geo_cache_expires(tmp_path, monkeypatch):
    cache_path = tmp_path / 'ip_locale.json'
    monkeypatch.setattr(browser_module, '_GEO_CACHE_PATH', str(cache_path))
    _geo_cache_write({'country_code': 'TH', 'timezone': 'Asia/Bangkok'})
    payload = json.loads(cache_path.read_text())
    payload['fetched_at'] = time.time() - browser_module._GEO_CACHE_TTL_S - 10
    cache_path.write_text(json.dumps(payload))
    assert _geo_cache_read() is None


def test_geo_cache_ignores_corrupt_payload(tmp_path, monkeypatch):
    cache_path = tmp_path / 'ip_locale.json'
    monkeypatch.setattr(browser_module, '_GEO_CACHE_PATH', str(cache_path))
    cache_path.write_text('not json at all')
    assert _geo_cache_read() is None
    cache_path.write_text(json.dumps({'fetched_at': time.time(), 'data': {}}))
    assert _geo_cache_read() is None


def test_geo_cache_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, '_GEO_CACHE_PATH', str(tmp_path / 'nope.json'))
    assert _geo_cache_read() is None


def test_detect_ip_locale_uses_disk_cache_without_network(tmp_path, monkeypatch):
    # regression: the cached path used to raise on the identity log line
    # (undefined `source`), which failed the first fetch after a restart
    monkeypatch.setattr(browser_module, '_GEO_CACHE_PATH', str(tmp_path / 'ip_locale.json'))
    _geo_cache_write({'country_code': 'TH', 'timezone': 'Asia/Bangkok'})
    monkeypatch.setattr(browser_module, '_ip_locale_cache', None)

    import urllib.request

    def no_network(*args, **kwargs):
        raise AssertionError('a fresh cache must not trigger a geo lookup')

    monkeypatch.setattr(urllib.request, 'urlopen', no_network)
    locale = browser_module._detect_ip_locale()
    assert locale['locale'] == 'th-TH'
    assert 'cache' in locale['source']
    assert browser_module._ip_locale_cache is locale


# per-lane Xvfb displays: one pointer per lane, no cross-window click theft


def test_lane_display_number_assigns_one_display_per_lane():
    assert browser_module._lane_display_number(0) == 99
    assert browser_module._lane_display_number(1) == 100
    assert browser_module._lane_display_number(5) == 104


def test_lane_display_number_never_negative():
    assert browser_module._lane_display_number(-3) == 99


def test_xpointer_key_entry_maps_latin1_through_the_keymap():
    # no X server here: build the object without __init__ and feed a
    # minimal keymap (keysym -> (keycode, shift required))
    pointer = human_input_module._XPointer.__new__(human_input_module._XPointer)
    pointer._keymap = {
        0x61: (38, False),  # 'a'
        0x41: (38, True),  # 'A' (shifted column of the same key)
        0x21: (10, True),  # '!'
        0x20: (65, False),  # space
    }
    assert pointer._key_entry('a') == (38, False)
    assert pointer._key_entry('A') == (38, True)
    assert pointer._key_entry('!') == (10, True)
    assert pointer._key_entry(' ') == (65, False)


def test_xpointer_key_entry_rejects_off_keymap_characters():
    pointer = human_input_module._XPointer.__new__(human_input_module._XPointer)
    pointer._keymap = {}
    # non-Latin-1 (Thai) and multi-character input are not typable: the
    # caller falls back to page-level key events
    assert pointer._key_entry('\u0e01') is None
    assert pointer._key_entry('ab') is None
    assert pointer._key_entry('') is None


def test_human_session_requires_a_display():
    async def check():
        with pytest.raises(human_input_module.HumanInputError):
            async with human_input_module.human_session(None):
                pass

    asyncio.run(check())


# --------------------------------------------------------------------------
# persistent per-lane profiles (outgoing.browser_profile_dir)


class _FakeContext:
    """Just enough of a BrowserContext for the aliveness check."""

    def __init__(self, closed: bool):
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


def test_lane_profile_dir_unset_runs_ephemeral():
    pool = browser_module.BrowserFetchPool(profile_dir=None)
    assert pool._lane_profile_dir(0) is None


def test_lane_profile_dir_creates_one_dir_per_lane(tmp_path):
    pool = browser_module.BrowserFetchPool(profile_dir=str(tmp_path))
    first = pool._lane_profile_dir(2)
    second = pool._lane_profile_dir(5)
    assert first == str(tmp_path / "lane-2")
    assert second == str(tmp_path / "lane-5")
    assert (tmp_path / "lane-2").is_dir()
    assert (tmp_path / "lane-5").is_dir()
    # distinct lanes never share a profile
    assert first != second


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_lane_profile_dir_unwritable_falls_back_ephemeral(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        pool = browser_module.BrowserFetchPool(profile_dir=str(locked))
        assert pool._lane_profile_dir(0) is None
    finally:
        locked.chmod(0o700)


def test_lane_is_alive_handles_persistent_contexts():
    alive = browser_module._Lane(None, _FakeContext(closed=False), None)
    dead = browser_module._Lane(None, _FakeContext(closed=True), None)
    assert browser_module.BrowserFetchPool._lane_is_alive(alive) is True
    assert browser_module.BrowserFetchPool._lane_is_alive(dead) is False


# --------------------------------------------------------------------------
# post-search reputation browsing


def _serp_response(status=200, url="https://www.bing.com/search?q=test",
                   content_type="text/html; charset=utf-8"):
    return browser_module.BrowserResponse(
        status_code=status,
        headers={"content-type": content_type},
        content=b"<html></html>",
        url=url,
        method="GET",
    )


def _lane_stub():
    return browser_module._Lane(None, None, None)


def test_browsable_links_keeps_offsite_and_engine_wrappers():
    serp = "https://www.bing.com/search?q=linux"
    out = browser_module._browsable_links(
        [
            "https://www.bing.com/ck/a?!&u=a1&ntb=1",  # organic wrapper
            "https://en.wikipedia.org/wiki/Linux",  # off-site result
            "https://www.bing.com/images/search?q=linux",  # vertical
            "/search?q=related+searches",  # related search
            "https://www.bing.com/account/general",  # settings
            "javascript:void(0)",
            "mailto:x@y.z",
            "#",
            "",
            None,
        ],
        serp,
    )
    assert "https://www.bing.com/ck/a?!&u=a1&ntb=1" in out
    assert "https://en.wikipedia.org/wiki/Linux" in out
    assert len(out) == 2


def test_browsable_links_keeps_google_goto_wrappers():
    serp = "https://www.google.com/search?q=linux"
    out = browser_module._browsable_links(
        [
            "/goto?url=CAESZAHrOzAVHB0og9NqrORWjur2zmOTzwdJe1Vj5Y",  # organic wrapper
            "/search?q=related+searches",  # related search
            "https://www.google.com/preferences",  # settings
            "https://www.google.com/intl/en/about/products",  # footer
        ],
        serp,
    )
    assert out == ["/goto?url=CAESZAHrOzAVHB0og9NqrORWjur2zmOTzwdJe1Vj5Y"]


def test_browsable_links_drops_google_translate_targets():
    serp = "https://www.google.com/search?q=linux"
    out = browser_module._browsable_links(
        [
            "https://energysavingtrust-org-uk.translate.goog/advice/x",  # translate
            "https://translate.google.com/translate?u=x",  # legacy translate
            "https://example.org/page",
        ],
        serp,
    )
    assert out == ["https://example.org/page"]


def test_unwrap_google_translate_url():
    from searx.network.browser import unwrap_google_translate_url

    assert (
        unwrap_google_translate_url(
            "https://energysavingtrust-org-uk.translate.goog/advice/x"
            "?_x_tr_sl=en&_x_tr_tl=th"
        )
        == "https://energysavingtrust.org.uk/advice/x"
    )
    assert (
        unwrap_google_translate_url(
            "https://en-m-wikipedia-org.translate.goog/wiki/Heat_pump"
            "?_x_tr_sl=en&_x_tr_tl=th&keep=1"
        )
        == "https://m.wikipedia.org/wiki/Heat_pump?keep=1"
    )
    assert unwrap_google_translate_url("https://example.org/x") == "https://example.org/x"


def test_lane_serp_url_affinity_routing():
    """A crawl of a URL a lane's search returned routes back to that lane."""
    from types import SimpleNamespace as NS

    pool = browser_module.BrowserFetchPool(pool_size=2)
    lane_a = browser_module._Lane(None, None, ":110")
    lane_b = browser_module._Lane(None, None, ":111")
    pool._lanes = [lane_a, lane_b]
    pool._lane_cycle = __import__("asyncio").Queue()
    for lane in pool._lanes:
        pool._lane_cycle.put_nowait(lane)

    target = "https://www.google.com/goto?url=CAESZAHrOzAVHB0og9NqrORWjur2zmOTzwdJe1Vj5Y"
    lane_b.serp_urls.append(target)

    assert pool._lane_for_serp_url(target) is lane_b
    assert pool._lane_for_serp_url("https://unrelated.example.org/x") is None

    # claim the finding lane: it must come out of the cycle, the other lane
    # must stay queued, and the claimed lane must be marked busy
    claimed = pool._claim_lane_now(lane_b)
    assert claimed is lane_b
    assert lane_b.busy is True
    assert pool._lane_cycle.qsize() == 1
    assert pool._lane_cycle.get_nowait() is lane_a


def test_checkout_for_crawl_borrows_busy_affinity_lane():
    """When the finding lane is mid-request/browsing, the crawl rides that
    same browser instead of losing affinity -- and must not hand the lane
    back to the cycle afterwards."""
    import asyncio
    from types import SimpleNamespace as NS

    pool = browser_module.BrowserFetchPool(pool_size=2)
    lane_a = browser_module._Lane(None, None, ":110")
    lane_b = browser_module._Lane(None, None, ":111")
    pool._lanes = [lane_a, lane_b]
    pool._lane_cycle = asyncio.Queue()
    pool._init_done = True  # fake lanes: skip the real pool init
    pool._ensure_browser_alive = lambda: asyncio.sleep(0)  # noqa: ARG005
    lane_b.browser = NS(is_connected=lambda: True)

    target = "https://www.google.com/goto?url=CAESZAHrOzAVHB0og9NqrORWjur2zmOTzwdJe1Vj5Y"
    lane_b.serp_urls.append(target)

    # simulate lane_b being checked out by a browsing session
    pool._lane_cycle.get_nowait()  # drop lane order: both out
    pool._lane_cycle.get_nowait()
    lane_b.busy = True

    async def _run():
        return await pool._checkout_for_crawl(target)

    lane, affinity, borrowed = asyncio.new_event_loop().run_until_complete(_run())
    assert lane is lane_b
    assert affinity is True
    assert borrowed is True
    # the borrowed lane was NOT returned to the cycle: it stays with its
    # original owner (the browsing session)
    assert pool._lane_cycle.qsize() == 0
    assert lane_b.busy is True


def test_record_serp_urls_keeps_organic_links_only():
    from types import SimpleNamespace as NS

    pool = browser_module.BrowserFetchPool(pool_size=1)
    lane = browser_module._Lane(None, None, ":110")
    response = NS(
        status_code=200,
        url="https://www.google.com/search?q=test",
        text=(
            '<a href="/goto?url=CAESZAHrOzA">t</a>'
            '<a href="https://example.org/page">t</a>'
            '<a href="/search?q=related">t</a>'
            '<a href="/preferences">t</a>'
            '<a href="/goto?url=CAESZAHrOzA">dup</a>'
        ),
    )
    pool._record_serp_urls(lane, "https://www.google.com/search?q=test", response)
    assert list(lane.serp_urls) == [
        "https://www.google.com/goto?url=CAESZAHrOzA",
        "https://example.org/page",
    ]


def test_record_serp_urls_ignores_non_search_and_errors():
    from types import SimpleNamespace as NS

    pool = browser_module.BrowserFetchPool(pool_size=1)
    lane = browser_module._Lane(None, None, ":110")
    ok = NS(status_code=200, url="https://example.org/page", text='<a href="https://x.org/a">')
    pool._record_serp_urls(lane, "https://example.org/page", ok)
    assert not lane.serp_urls  # not a search URL
    err = NS(status_code=500, url="https://www.google.com/search?q=x", text='<a href="https://x.org/a">')
    pool._record_serp_urls(lane, "https://www.google.com/search?q=x", err)
    assert not lane.serp_urls  # failed search


def test_is_google_translate_url():
    assert browser_module._is_google_translate_url(
        "https://www-iea-org.translate.goog/reports/x?_x_tr_sl=en"
    )
    assert browser_module._is_google_translate_url("https://translate.google.com/x")
    assert not browser_module._is_google_translate_url("https://example.org/x")
    assert not browser_module._is_google_translate_url(None)


def test_browsable_links_dedups_identical_wrappers_and_keeps_first_raw():
    serp = "https://duckduckgo.com/?q=linux&ia=web"
    wrap = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F&rut=abc"
    out = browser_module._browsable_links(
        [
            wrap,
            "/l/?uddg=https%3A%2F%2Fexample.com%2F&rut=abc",  # same target
            "https://example.org/page",
        ],
        serp,
    )
    assert len(out) == 2
    assert out[0] == wrap  # raw attribute value preserved for the locator


def test_browsable_links_drops_assets_and_caps_candidates():
    serp = "https://www.bing.com/search?q=x"
    assert browser_module._browsable_links(
        ["https://cdn.example.com/photo.jpg"], serp
    ) == []
    many = [f"https://site{i}.example.com/page" for i in range(100)]
    out = browser_module._browsable_links(many, serp)
    assert len(out) == browser_module._POST_SEARCH_MAX_LINKS


def test_registrable_site():
    assert browser_module._registrable_site("www.bing.com") == "bing.com"
    assert browser_module._registrable_site("localhost") == "localhost"
    assert browser_module._registrable_site(None) == ""


def test_link_locator_escapes_attribute_value():
    selectors = []

    class FakePage:
        def locator(self, selector):
            selectors.append(selector)
            return SimpleNamespace(first="locator")

    loc = browser_module._link_locator(FakePage(), 'https://x.com/a"b\\c')
    assert loc == "locator"
    assert selectors == ['a[href="https://x.com/a\\"b\\\\c"]']


def test_post_search_browsing_spawn_gate():
    async def check():
        pool = browser_module.BrowserFetchPool(post_search_browsing=True)
        pool._lane_cycle = asyncio.Queue()

        async def noop_session(_lane, _page, _serp_url):
            return

        pool._post_search_browsing_session = noop_session
        # non-HTML or failing responses never spawn
        assert await pool._maybe_start_post_search_browsing(
            _lane_stub(), _serp_response(status=403)
        ) is False
        assert await pool._maybe_start_post_search_browsing(
            _lane_stub(), _serp_response(content_type="application/json")
        ) is False
        # no idle lane: browsing is skipped, search keeps the capacity
        assert await pool._maybe_start_post_search_browsing(
            _lane_stub(), _serp_response()
        ) is False
        # good response with a spare lane: spawn and hold the lane
        pool._lane_cycle.put_nowait(object())
        lane = _lane_stub()
        assert await pool._maybe_start_post_search_browsing(
            lane, _serp_response()
        ) is True

    asyncio.run(check())


def test_post_search_browsing_disabled_by_default():
    async def check():
        pool = browser_module.BrowserFetchPool()
        pool._lane_cycle = asyncio.Queue()
        pool._lane_cycle.put_nowait(object())
        assert await pool._maybe_start_post_search_browsing(
            _lane_stub(), _serp_response()
        ) is False

    asyncio.run(check())


def test_browsable_links_drops_wrappers_resolving_insite():
    serp = "https://www.bing.com/search?q=x"

    def wrap(target: str) -> str:
        u = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        return f"https://www.bing.com/ck/a?!&&u={u}&ntb=1"

    out = browser_module._browsable_links(
        [
            wrap("https://www.bing.com/images/search?q=y"),  # stays on bing
            wrap("https://en.wikipedia.org/wiki/Linux"),  # real result
            "https://example.org/page",
        ],
        serp,
    )
    assert out == [
        wrap("https://en.wikipedia.org/wiki/Linux"),
        "https://example.org/page",
    ]


def test_browsable_links_keeps_wrappers_with_unparseable_targets():
    serp = "https://www.bing.com/search?q=x"
    href = "https://www.bing.com/ck/a?!&&p=deadbeef&ntb=1"  # no u= payload
    assert browser_module._browsable_links([href], serp) == [href]



def test_post_search_wanted_requires_idle_lane():
    pool = browser_module.BrowserFetchPool(post_search_browsing=True)
    pool._lane_cycle = asyncio.Queue()
    assert pool._post_search_wanted("https://www.bing.com/search?q=x") is False
    pool._lane_cycle.put_nowait(object())
    assert pool._post_search_wanted("https://www.bing.com/search?q=x") is True
    assert pool._post_search_wanted("https://www.bing.com/ac/?q=x") is False
