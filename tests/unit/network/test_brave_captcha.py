# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brave's own challenge: route detection, PoW wait, slider drag.

Brave challenges flagged clients with its own SPA page (/captcha) instead
of a third-party widget: an invisible PoW runs first and reloads the
search on success; a slider puzzle is mounted only when the PoW is
refused. The solver must bail fast on pages that are not this challenge,
wait out a self-clearing PoW, and drag the knob with real X input -- the
vision model only enters when plain end-drags are refused.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from searx.network import human_input
from searx.network.browser import _is_challenge_url
from searx.network.human_input import (
    _on_brave_challenge_route,
    human_clear_challenge,
    human_solve_brave_captcha,
)

# lane window geometry for _to_screen: viewport at the screen origin,
# 80px of browser chrome at the top (screen y = page y + 80)
_WINDOW_GEO = {"sx": 0, "sy": 0, "ow": 1440, "oh": 900, "iw": 1440, "ih": 820}

_TRACK_BOX = {"x": 440.0, "y": 480.0, "width": 320.0, "height": 40.0}
_KNOB_BOX = {"x": 440.0, "y": 484.0, "width": 44.0, "height": 32.0}
_KNOB_CENTER = (462, 580)  # screen coords of the knob center
_TRAVEL = 276.0  # track width - knob width


class FakePointer:
    """Just enough of _XPointer for the drag primitives."""

    def __init__(self):
        self._pos = (10, 10)
        self.events = []

    def size(self):
        return (1920, 1080)

    def position(self):
        return self._pos

    def move_to(self, x, y):
        self._pos = (int(x), int(y))
        self.events.append(("move", int(x), int(y)))

    def mouse_down(self):
        self.events.append(("down",))

    def mouse_up(self):
        self.events.append(("up",))


class FakeLocator:
    """A fixed or scripted element: box, visibility, optional hide hook."""

    def __init__(self, box=None, visible=True, on_hidden=None):
        self._box = box
        self._visible = visible
        self._on_hidden = on_hidden

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def is_visible(self):
        if isinstance(self._visible, list):
            if len(self._visible) > 1:
                return bool(self._visible.pop(0))
            value = bool(self._visible[0])
            if not value and self._on_hidden is not None:
                callback, self._on_hidden = self._on_hidden, None
                callback()
            return value
        return bool(self._visible)

    async def bounding_box(self):
        return self._box

    async def screenshot(self, **_kwargs):
        return b"canvas-png"


class FakePage:
    def __init__(self, url="https://search.brave.com/search?q=x"):
        self.url = url
        self.locators = {}

    def add(self, selector, box=None, visible=True):
        self.locators[selector] = FakeLocator(box=box, visible=visible)

    def remove(self, *selectors):
        for selector in selectors:
            self.locators.pop(selector, None)

    def locator(self, selector):
        return self.locators.get(selector, FakeLocator(box=None, visible=False))

    async def bring_to_front(self):
        pass

    async def evaluate(self, _script=None, *_args):
        return dict(_WINDOW_GEO)


@pytest.fixture
def fast_sleep():
    """Make every await sleep in human_input instant."""
    with patch(
        "searx.network.human_input.asyncio.sleep", new=AsyncMock(return_value=None)
    ):
        yield


def _run(check):
    asyncio.run(check())


def _challenge_page():
    page = FakePage(url="https://search.brave.com/captcha")
    page.add(
        ".captcha-card", box={"x": 400.0, "y": 300.0, "width": 400.0, "height": 300.0}
    )
    page.add(".captcha-slider", box=dict(_TRACK_BOX))
    page.add(".captcha-slider-button", box=dict(_KNOB_BOX))
    return page


def test_brave_route_detection():
    assert _on_brave_challenge_route("https://search.brave.com/captcha")
    assert _on_brave_challenge_route(
        "https://search.brave.com/captcha/slider?reload_with_fallback_captcha=abc"
    )
    assert not _on_brave_challenge_route("https://search.brave.com/search?q=captcha")
    # a page whose URL merely contains "captcha" is not the challenge
    assert not _on_brave_challenge_route("https://en.wikipedia.org/wiki/CAPTCHA")


def test_challenge_url_detection():
    assert _is_challenge_url("https://search.brave.com/captcha")
    assert _is_challenge_url("https://search.brave.com/captcha/slider")
    assert _is_challenge_url("https://www.google.com/sorry/index?continue=x")
    assert not _is_challenge_url("https://search.brave.com/search?q=captcha")
    assert not _is_challenge_url("https://en.wikipedia.org/wiki/CAPTCHA")
    assert not _is_challenge_url("https://duckduckgo.com/?q=x")


def test_bails_fast_off_challenge(fast_sleep):
    page = FakePage(url="https://search.brave.com/search?q=x")
    pointer = FakePointer()

    async def check():
        assert not await human_solve_brave_captcha(page, pointer, settle_ms=1500)

    _run(check)
    assert pointer.events == []  # no drag attempted


def test_pow_self_clear(fast_sleep):
    page = FakePage(url="https://search.brave.com/captcha")
    # widget visible on the mount check and the first PoW poll, then gone
    page.add(".captcha-card", visible=[True, True, False])
    pointer = FakePointer()

    async def check():
        with patch.object(human_input, "_BRAVE_POW_WAIT_S", 2.0):
            solved = await human_solve_brave_captcha(page, pointer)
        assert solved

    _run(check)
    assert pointer.events == []  # cleared without input


def test_slider_drag_solves(fast_sleep):
    page = _challenge_page()
    pointer = FakePointer()
    original_up = pointer.mouse_up

    def up():
        original_up()
        # the SPA verifies and reloads: the widget unmounts
        page.remove(".captcha-card", ".captcha-slider", ".captcha-slider-button")
        page.url = "https://search.brave.com/search?q=x"

    pointer.mouse_up = up

    async def check():
        with patch.object(human_input, "_BRAVE_POW_WAIT_S", 2.0):
            solved = await human_solve_brave_captcha(page, pointer)
        assert solved

    _run(check)
    downs = [event for event in pointer.events if event[0] == "down"]
    ups = [event for event in pointer.events if event[0] == "up"]
    assert len(downs) == 1 and len(ups) == 1
    moves = [event for event in pointer.events if event[0] == "move"]
    final_x, final_y = moves[-1][1], moves[-1][2]
    # end-drag: knob center + full travel (738), with +-2 release jitter
    assert 736 <= final_x <= 740
    assert 576 <= final_y <= 584
    # the button was held across the drag: approach move, down, drag move, up
    assert pointer.events.index(("down",)) > 0  # approached before pressing
    assert pointer.events.index(("up",)) == len(pointer.events) - 1
    assert pointer.events.index(("down",)) < len(moves)  # drag move after down


def test_vision_fallback_after_refused_end_drag(fast_sleep):
    page = _challenge_page()
    pointer = FakePointer()
    ups_seen = []
    original_up = pointer.mouse_up

    def up():
        original_up()
        ups_seen.append(1)
        if len(ups_seen) >= 2:
            # only the gap-aligned drag verifies
            page.remove(".captcha-card", ".captcha-slider", ".captcha-slider-button")
            page.url = "https://search.brave.com/search?q=x"

    pointer.mouse_up = up

    solver = MagicMock()
    solver.chat_vision.return_value = "0.42"

    async def check():
        with (
            patch.object(human_input, "_BRAVE_POW_WAIT_S", 2.0),
            patch.object(human_input, "_BRAVE_CLEAR_WAIT_S", 0.5),
            patch(
                "searx.network.captcha_vision.get_vision_solver", return_value=solver
            ),
        ):
            solved = await human_solve_brave_captcha(page, pointer)
        assert solved

    _run(check)
    solver.chat_vision.assert_called_once()
    prompt, images = solver.chat_vision.call_args[0]
    assert "gap" in prompt
    assert images == [b"canvas-png"]
    downs = [event for event in pointer.events if event[0] == "down"]
    ups = [event for event in pointer.events if event[0] == "up"]
    assert len(downs) == 2 and len(ups) == 2
    moves = [event for event in pointer.events if event[0] == "move"]
    final_x = moves[-1][1]
    # fraction 0.42 of the travel: 462 + 0.42 * 276 = 577.92
    assert 575 <= final_x <= 581


def test_no_vision_solver_aborts_after_refused_end_drag(fast_sleep):
    page = _challenge_page()
    pointer = FakePointer()

    async def check():
        with (
            patch.object(human_input, "_BRAVE_POW_WAIT_S", 2.0),
            patch.object(human_input, "_BRAVE_CLEAR_WAIT_S", 0.5),
            patch(
                "searx.network.captcha_vision.get_vision_solver", return_value=None
            ),
        ):
            solved = await human_solve_brave_captcha(page, pointer)
        assert not solved

    _run(check)
    downs = [event for event in pointer.events if event[0] == "down"]
    ups = [event for event in pointer.events if event[0] == "up"]
    assert len(downs) == 1 and len(ups) == 1  # end-drag ran, then gave up


def test_clear_challenge_delegates_to_brave():
    page = FakePage()
    pointer = FakePointer()

    async def check():
        with (
            patch.object(
                human_input, "human_solve_challenge", new=AsyncMock(return_value=False)
            ),
            patch.object(
                human_input,
                "human_solve_image_challenge",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                human_input,
                "human_solve_brave_captcha",
                new=AsyncMock(return_value=True),
            ) as brave,
        ):
            cleared = await human_clear_challenge(page, pointer)
        assert cleared
        brave.assert_awaited_once_with(page, pointer, settle_ms=6000)

    _run(check)
