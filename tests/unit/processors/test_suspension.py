# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for engine suspension: a TooManyRequests exception that
carries its message in ``suspended_time`` (as brave.py did) must suspend the
engine with the default time, not crash the search thread on
``default_timer() + suspended_time``."""

# pylint: disable=missing-module-docstring,missing-function-docstring
# pylint: disable=protected-access,invalid-name

from unittest.mock import MagicMock

from searx import engines
from searx.engines import brave
from searx.exceptions import SearxEngineTooManyRequestsException
from searx.search.models import EngineRef, SearchQuery
from searx.search.processors import online

from tests import SearxTestCase

TEST_ENGINE_NAME = "dummy engine"  # from the ./settings/test_settings.yml


class TestSuspension(SearxTestCase):

    def test_too_many_requests_with_message_in_suspended_time_does_not_crash(self):
        """The 2026-10-19 outage shape: brave raised
        TooManyRequestsException("<message>") -- the string landed in
        suspended_time, handle_exception passed it on, and the search
        thread died on float + str. It must suspend on the default
        instead."""
        engine = engines.engines[TEST_ENGINE_NAME]
        processor = online.OnlineProcessor(engine)

        search_query = SearchQuery('test', [EngineRef(TEST_ENGINE_NAME, 'general')], 'all', 0, 1, None, None, None)
        result_container = MagicMock()
        # raise for real: handle_exception inspects the active traceback
        try:
            raise SearxEngineTooManyRequestsException("challenge or block")
        except SearxEngineTooManyRequestsException as exception:
            assert isinstance(exception.suspended_time, str)  # the bug's shape
            processor.handle_exception(result_container, exception, suspend=True)
        assert processor.suspended_status.suspend_end_time is not None


def test_brave_challenge_page_raises_numeric_suspended_time():
    """A brave challenge page must yield a suspension exception whose
    suspended_time is not a message string."""
    resp = MagicMock()
    resp.text = "<html>challenge interstitial, no app-state script</html>"
    try:
        brave.response(resp)
    except SearxEngineTooManyRequestsException as exc:
        assert not isinstance(exc.suspended_time, str), exc.suspended_time
    else:
        raise AssertionError("brave.response did not raise on a challenge page")
