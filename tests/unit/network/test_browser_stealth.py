# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the max-stealth browser routing helpers."""

# pylint: disable=missing-module-docstring

import pytest

from searx.network.browser import (
    _homepage_url_from_search_url,
    _ip_locale_from_payload,
    _is_api_url,
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


# _ip_locale_from_payload: the lane identity must match the public IP


def test_ip_locale_from_ipapi_payload():
    locale = _ip_locale_from_payload(
        {
            'country_code': 'DE',
            'timezone': 'Europe/Berlin',
            'languages': 'de,de-DE',
            'latitude': 52.52,
            'longitude': 13.4,
        }
    )
    assert locale == {
        'locale': 'de-DE',
        'timezone': 'Europe/Berlin',
        'accept_language': 'de-DE,de;q=0.9,en;q=0.7',
        'geolocation': {'latitude': 52.52, 'longitude': 13.4, 'accuracy': 40.0},
    }


def test_ip_locale_from_ipinfo_payload():
    locale = _ip_locale_from_payload(
        {'country': 'JP', 'timezone': 'Asia/Tokyo', 'loc': '35.68,139.69'}
    )
    assert locale['locale'] == 'en-US'
    assert locale['timezone'] == 'Asia/Tokyo'
    assert locale['geolocation'] == {
        'latitude': 35.68,
        'longitude': 139.69,
        'accuracy': 40.0,
    }
    assert 'en' in locale['accept_language']


def test_ip_locale_survives_missing_coordinates():
    locale = _ip_locale_from_payload(
        {'country_code': 'FR', 'timezone': 'Europe/Paris'}
    )
    assert locale['geolocation'] is None
    assert locale['locale'] == 'en-US'
    assert locale['timezone'] == 'Europe/Paris'


def test_ip_locale_requires_a_country():
    assert _ip_locale_from_payload({'timezone': 'Europe/Berlin'}) is None
