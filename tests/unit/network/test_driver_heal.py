# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-healing of the browser pool's shared playwright driver.

The driver is one node process for the whole pool: when the container
OOM-kills it, every launch fails with ``Connection closed while reading
from the driver`` and lane restarts would loop forever on the dead
instance. A launch must rebuild the driver once and retry.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from searx.network.browser import BrowserFetchPool

_DRIVER_DEAD = "BrowserType.launch: Connection closed while reading from the driver"


@pytest.fixture
def pool():
    return BrowserFetchPool(pool_size=1)


@pytest.fixture
def fake_playwright():
    instance = MagicMock()
    with patch("playwright.async_api.async_playwright") as factory:
        # start() yields to the loop before resolving, so concurrent
        # callers actually interleave
        async def _start():
            await asyncio.sleep(0)
            return instance

        factory.return_value.start = AsyncMock(side_effect=_start)
        yield factory, instance


def _run(check) -> None:
    asyncio.run(check())


def test_launch_retries_after_driver_death(pool, fake_playwright):
    _, instance = fake_playwright
    attempts = []

    async def check():
        async def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise Exception(_DRIVER_DEAD)
            return "launched"

        result = await pool._launch_with_driver_heal(factory)
        assert result == "launched"
        assert len(attempts) == 2
        assert pool._playwright is instance

    _run(check)


def test_unrelated_launch_errors_propagate(pool, fake_playwright):
    async def check():
        async def factory():
            raise RuntimeError("chromium missing")

        with pytest.raises(RuntimeError):
            await pool._launch_with_driver_heal(factory)
        assert pool._playwright is None

    _run(check)


def test_restart_replaces_a_dead_instance(pool, fake_playwright):
    factory, instance = fake_playwright
    dead = MagicMock()
    dead.stop = AsyncMock()
    pool._playwright = dead

    async def check():
        await pool._restart_playwright()

    _run(check)
    dead.stop.assert_awaited_once()
    assert pool._playwright is instance
    factory.return_value.start.assert_awaited_once()


def test_concurrent_restarts_build_one_driver(pool, fake_playwright):
    factory, instance = fake_playwright
    dead = MagicMock()

    # the dead driver's stop yields before failing: the second caller
    # gets to run while the first is mid-rebuild
    async def _stop():
        await asyncio.sleep(0)
        raise RuntimeError("transport closed")

    dead.stop = AsyncMock(side_effect=_stop)
    pool._playwright = dead

    async def check():
        await asyncio.gather(pool._restart_playwright(), pool._restart_playwright())

    _run(check)
    factory.return_value.start.assert_awaited_once()
    assert pool._playwright is instance


def test_driver_death_is_recognized_by_message():
    dead = Exception("BrowserContext.new_page: Connection closed while reading from the driver")
    assert BrowserFetchPool._driver_died(dead)
    assert not BrowserFetchPool._driver_died(Exception("Target closed"))
    assert not BrowserFetchPool._driver_died(Exception("chromium missing"))


def test_rebuild_marks_lanes_stale(pool, fake_playwright):
    factory, instance = fake_playwright
    dead = MagicMock()
    dead.stop = AsyncMock()
    pool._playwright = dead

    async def check():
        await pool._restart_playwright()

    _run(check)
    assert pool._lanes_stale


def test_ensure_alive_restarts_lanes_that_lie(pool, fake_playwright):
    # after a driver death every lane still reports itself connected:
    # the stale flag, not the flag check, must drive the restart
    liar = MagicMock()
    liar.browser.is_connected.return_value = True
    pool._lanes = [liar]
    pool._lanes_stale = True
    pool._restart_lane = AsyncMock()

    async def check():
        await pool._ensure_browser_alive()

    _run(check)
    pool._restart_lane.assert_awaited_once_with(liar)
    assert not pool._lanes_stale


def test_heal_without_stale_lanes_is_noop(pool, fake_playwright):
    liar = MagicMock()
    liar.browser.is_connected.return_value = True
    pool._lanes = [liar]
    pool._lanes_stale = False
    pool._restart_lane = AsyncMock()

    async def check():
        await pool._heal_after_driver_death()

    _run(check)
    pool._restart_lane.assert_not_awaited()
