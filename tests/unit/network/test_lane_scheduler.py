# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the per-lane work registry (_LaneScheduler)."""

# pylint: disable=missing-module-docstring, protected-access

from types import SimpleNamespace

from searx.network.browser import _LaneScheduler


def _lane(name="lane0"):
    return SimpleNamespace(display=name)


class TestSearchRegistration:
    def test_pending_while_waiting(self):
        scheduler = _LaneScheduler()
        scheduler.enter_search()
        assert scheduler.pending_work()
        assert scheduler.work_pending_for(_lane())
        scheduler.exit_search()
        assert not scheduler.pending_work()

    def test_exit_without_enter_is_safe(self):
        scheduler = _LaneScheduler()
        scheduler.exit_search()
        scheduler.exit_search()
        assert not scheduler.pending_work()


class TestCrawlRegistration:
    def test_no_affinity_crawl_yields_every_browsing_lane(self):
        scheduler = _LaneScheduler()
        lane_a, lane_b = _lane("a"), _lane("b")
        scheduler.enter_crawl(None)
        assert scheduler.pending_work()
        assert scheduler.work_pending_for(lane_a)
        assert scheduler.work_pending_for(lane_b)
        scheduler.exit_crawl(None)

    def test_affinity_crawl_yields_only_its_lane(self):
        scheduler = _LaneScheduler()
        lane_a, lane_b = _lane("a"), _lane("b")
        scheduler.enter_crawl(lane_a)
        assert scheduler.work_pending_for(lane_a)
        assert not scheduler.work_pending_for(lane_b)
        # but the pool as a whole has pending work: a new SERP skips
        # starting a browsing session at all
        assert scheduler.pending_work()
        scheduler.exit_crawl(lane_a)
        assert not scheduler.work_pending_for(lane_a)

    def test_counts_are_refcounted(self):
        scheduler = _LaneScheduler()
        lane = _lane()
        scheduler.enter_crawl(lane)
        scheduler.enter_crawl(lane)
        scheduler.exit_crawl(lane)
        assert scheduler.work_pending_for(lane)
        scheduler.exit_crawl(lane)
        assert not scheduler.work_pending_for(lane)

    def test_search_outranks_affinity(self):
        """A waiting search yields every lane, even one whose only pending
        work was an affinity crawl."""
        scheduler = _LaneScheduler()
        lane = _lane()
        scheduler.enter_crawl(lane)
        assert scheduler.work_pending_for(lane)
        scheduler.exit_crawl(lane)
        scheduler.enter_search()
        assert scheduler.work_pending_for(lane)
