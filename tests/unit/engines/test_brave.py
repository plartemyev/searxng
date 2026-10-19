# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parser robustness of the Brave engine against non-response pages.

A rate-limited query renders the same SPA shell with a ``challengeSet``
payload instead of results (and ``data[1]`` is ``null``): the engine must
report a rate limit (so the processor suspends it) rather than raise a
structure error that retries into the same wall.
"""

import pytest

from searx.engines.brave import _get_response_data, extract_json_data
from searx.exceptions import SearxEngineTooManyRequestsException

_CHALLENGE_NODE = (
    'null, {type:"data",data:{challengeSet:{set_token:"tok",tokens:["a","b"]}}}'
)

_RESPONSE_NODE = (
    '{type:"data",data:{noResults:false,body:{response:{web:{results:'
    '[{title:"t",url:"https://x.org",description:"d"}]}}}}}'
)


def _kit_html(payload: str) -> str:
    # the kit line must carry the whole array on ONE line: extract slices
    # from "data: [{" to the last "}}]" before the newline
    return (
        '<html><script>kit.start(app, el, {\n'
        ' data: [{type:"data",data:{userAgent:{uaString:"Mozilla/5.0"},'
        'query:{text:"test"}}}, ' + payload + '],\n'
        ' form: null,\n error: null\n});</script></html>'
    )


def test_challenge_page_raises_rate_limit():
    data = extract_json_data(_kit_html(_CHALLENGE_NODE))
    with pytest.raises(SearxEngineTooManyRequestsException):
        _get_response_data(data, "search")


def test_page_without_app_state_raises_rate_limit():
    with pytest.raises(SearxEngineTooManyRequestsException):
        extract_json_data("<html><body>access denied</body></html>")


_IIFE_PAYLOAD = (
    '{type:"data",data:{noResults:false,body:{response:{web:{results:'
    '[{title:"t",url:"https://x.org",description:\'write "return {" and "]" to trip it\'}]}}}}}'
)


def _iife_kit_html(payload: str) -> str:
    # the real 2026-10 page: elements are functions, not called inline
    return (
        '<html><script>kit.start(app, el, {\n'
        ' data: [(function(a,b){return {type:"data",data:{userAgent:{uaString:"x"},'
        'query:{text:"test"}}}}), (function(c,d){return ' + payload + '})],\n'
        ' form: null,\n error: null\n});</script></html>'
    )


def test_iife_payload_parses():
    data = extract_json_data(_iife_kit_html(_IIFE_PAYLOAD))
    response = _get_response_data(data, "search")
    assert response["results"][0]["title"] == "t"
    assert response["results"][0]["url"] == "https://x.org"
    # strings inside the payload are data, not code
    assert "return {" in response["results"][0]["description"]


def test_iife_challenge_raises_rate_limit():
    challenge = 'null, {type:"data",data:{challengeSet:{set_token:"tok",tokens:["a","b"]}}}'
    data = extract_json_data(_iife_kit_html(challenge))
    with pytest.raises(SearxEngineTooManyRequestsException):
        _get_response_data(data, "search")


def test_healthy_serp_still_parses():
    data = extract_json_data(_kit_html(_RESPONSE_NODE))
    web = _get_response_data(data, "search")
    assert len(web["results"]) == 1
    assert web["results"][0]["url"] == "https://x.org"
