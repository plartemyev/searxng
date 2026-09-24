# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name

import asyncio
import threading
import unittest.mock as mock

import searx.network.pacing as pacing
from searx.network.pacing import pace_key_for_url, reserve_send_slot
from tests import SearxTestCase


def _clear_slots():
    with pacing._slot_lock:  # pylint: disable=protected-access
        pacing._next_slot_by_key.clear()  # pylint: disable=protected-access


def _patch_settings(delay_min=1.0, delay_max=5.0, max_wait=20.0, enabled=True):
    values = {
        'outgoing.pacing.enabled': enabled,
        'outgoing.pacing.delay_min_seconds': delay_min,
        'outgoing.pacing.delay_max_seconds': delay_max,
        'outgoing.pacing.max_wait_seconds': max_wait,
    }
    return mock.patch.object(pacing, 'get_setting', side_effect=lambda name, default=None: values[name])


class PaceKeyTest(SearxTestCase):
    def test_host_is_stripped_of_www(self):
        self.assertEqual(pace_key_for_url('https://www.google.com/search?q=x'), 'google.com')
        self.assertEqual(pace_key_for_url('https://google.com/search?q=x'), 'google.com')

    def test_port_and_case_do_not_split_the_key(self):
        self.assertEqual(pace_key_for_url('https://WWW.Example.com:8080/x'), 'example.com')

    def test_empty_host(self):
        self.assertEqual(pace_key_for_url('not a url'), '')


class ReserveSendSlotTest(SearxTestCase):
    def setUp(self):
        super().setUp()
        _clear_slots()

    def test_first_request_is_not_delayed(self):
        wait = reserve_send_slot('google.com', delay_min=1, delay_max=5, max_wait=20)
        self.assertEqual(wait, 0.0)

    def test_second_request_is_delayed_by_a_random_gap(self):
        # deterministic 1s gaps: the first re-arms the chain 1s ahead, the
        # second sends one gap after that slot
        reserve_send_slot('google.com', delay_min=1, delay_max=1, max_wait=20)
        wait = reserve_send_slot('google.com', delay_min=1, delay_max=1, max_wait=20)
        self.assertAlmostEqual(wait, 2.0, delta=0.2)

    def test_slots_chain_across_many_requests(self):
        waits = []
        for _ in range(5):
            waits.append(reserve_send_slot('google.com', delay_min=1, delay_max=2, max_wait=20))
        # the k-th concurrent request waits at least (k-1) * delay_min
        for position, wait in enumerate(waits):
            self.assertGreaterEqual(wait, position * 1.0)

    def test_different_providers_do_not_block_each_other(self):
        reserve_send_slot('google.com', delay_min=1, delay_max=5, max_wait=20)
        wait = reserve_send_slot('bing.com', delay_min=1, delay_max=5, max_wait=20)
        self.assertEqual(wait, 0.0)

    def test_max_wait_caps_the_queue(self):
        reserve_send_slot('google.com', delay_min=1, delay_max=5, max_wait=20)
        for _ in range(20):
            # queue many requests: none may wait longer than max_wait
            wait = reserve_send_slot('google.com', delay_min=1, delay_max=5, max_wait=20)
            self.assertLessEqual(wait, 20.0)

    def test_reserve_is_thread_safe(self):
        waits = []
        lock = threading.Lock()

        def reserve():
            for _ in range(50):
                wait = reserve_send_slot('google.com', delay_min=0.001, delay_max=0.002, max_wait=5)
                with lock:
                    waits.append(wait)

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # every concurrent reservation got a slot
        self.assertEqual(len(waits), 8 * 50)


class PaceRequestTest(SearxTestCase):
    def setUp(self):
        super().setUp()
        _clear_slots()

    def test_first_request_passes_without_sleep(self):
        with _patch_settings():
            wait = asyncio.run(pacing.pace_request('https://www.google.com/search?q=a'))
        self.assertEqual(wait, 0.0)

    def test_second_request_sleeps_for_a_slot(self):
        with _patch_settings(delay_min=0.01, delay_max=0.02):
            asyncio.run(pacing.pace_request('https://www.google.com/search?q=a'))
            wait = asyncio.run(pacing.pace_request('https://www.google.com/search?q=b'))
        # two chained gaps of 0.01..0.02 each
        self.assertGreaterEqual(wait, 0.02)
        self.assertLessEqual(wait, 0.04)

    def test_disabled_pacing_returns_immediately(self):
        with _patch_settings(enabled=False):
            wait = asyncio.run(pacing.pace_request('https://www.google.com/search?q=a'))
        self.assertEqual(wait, 0.0)

    def test_empty_host_is_never_paced(self):
        with _patch_settings():
            wait = asyncio.run(pacing.pace_request('not a url'))
        self.assertEqual(wait, 0.0)
