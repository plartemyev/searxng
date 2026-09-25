# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the max-stealth browser routing helpers."""

# pylint: disable=missing-module-docstring

import asyncio
import json
import time

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
