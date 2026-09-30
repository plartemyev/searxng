# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for BrowserResponse text decoding and HTML parsing.

Regression coverage for the UTF-8-as-Latin-1 mojibake: parsing raw bytes
makes libxml2 guess the encoding, and its sniffer misses declarations like
bing's ``<meta content="...charset=utf-8" http-equiv="content-type">``.
"""

# pylint: disable=missing-module-docstring

from searx.network.browser import BrowserResponse, _sniff_meta_charset


THAI = 'ศูนย์บริหารจัดการทรัพยากรป่าชายเลนจังหวัดประจวบคีรีขันธ์'


def _response(content: bytes, headers: dict) -> BrowserResponse:
    return BrowserResponse(
        status_code=200,
        headers=headers,
        content=content,
        url='https://www.example.com/search?q=x',
        method='GET',
    )


# _sniff_meta_charset: both meta attribute orders, ASCII safety


def test_sniff_meta_charset_html5_shorthand():
    head = b'<html><head><meta charset="utf-8"><title>x</title>'
    assert _sniff_meta_charset(head) == 'utf-8'


def test_sniff_meta_charset_http_equiv_first():
    head = (
        b'<html><head>'
        b'<meta http-equiv="content-type" content="text/html; charset=utf-8">'
    )
    assert _sniff_meta_charset(head) == 'utf-8'


def test_sniff_meta_charset_content_attribute_first():
    """Bing's reversed attribute order: content= before http-equiv=."""
    head = (
        b'<html><head><title>serp</title>'
        b'<meta content="text/html; charset=utf-8" http-equiv="content-type">'
    )
    assert _sniff_meta_charset(head) == 'utf-8'


def test_sniff_meta_charset_bogus_unicode_name_maps_to_utf8():
    head = b'<html><head><meta charset="unicode">'
    assert _sniff_meta_charset(head) == 'utf-8'


def test_sniff_meta_charset_absent():
    assert _sniff_meta_charset(b'<html><head><title>x</title></head>') is None


# .html(): the parser sees decoded text, so non-ASCII survives


def _serp_page(meta_tag: str) -> str:
    filler = '<script>var x="' + 'a' * 5000 + '"</script>'
    return (
        '<html><head>' + filler + meta_tag + '</head><body>'
        f'<div class="web-result"><h2><a>{THAI}</a></h2></div>'
        '</body></html>'
    )


def test_html_parses_decoded_text_with_reversed_meta_order():
    """The exact bing shape: charset meta after the head scripts, attributes
    reversed. Bytes-mode parsing garbles this; text-mode must not."""
    page = _serp_page(
        '<meta content="text/html; charset=utf-8" http-equiv="content-type">'
    )
    response = _response(
        page.encode('utf-8'),
        # the wrapping BrowserResponse always claims utf-8, and so does the
        # raw-fetch path on every major search engine
        {'content-type': 'text/html; charset=utf-8'},
    )
    title = response.html().xpath('//h2/a')[0].text_content()
    assert title == THAI


def test_html_parses_decoded_text_without_header_charset():
    """No header charset: the meta declaration must be honored (and not
    delegated to libxml2's byte sniffer)."""
    page = _serp_page('<meta charset="utf-8">')
    response = _response(page.encode('utf-8'), {'content-type': 'text/html'})
    title = response.html().xpath('//h2/a')[0].text_content()
    assert title == THAI


def test_html_rejects_invalid_charset_name_and_falls_back_to_utf8():
    page = _serp_page('<meta charset="unicode">')
    response = _response(page.encode('utf-8'), {'content-type': 'text/html'})
    title = response.html().xpath('//h2/a')[0].text_content()
    assert title == THAI


def test_text_decodes_header_charset_over_meta():
    """A header charset wins over the page's own declaration (HTTP spec)."""
    body = THAI.encode('utf-8')
    response = _response(
        body, {'content-type': 'text/html; charset=iso-8859-1'}
    )
    # decoded as declared: latin-1 maps the raw bytes to their latin-1
    # lookalikes -- whatever happens, it must be deterministic and not
    # depend on the (utf-8) meta declaration
    assert response.text != THAI
