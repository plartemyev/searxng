# SPDX-License-Identifier: AGPL-3.0-or-later
"""Human-like input on the X display for the browser fetch pool.

Bot detectors score more than TLS and headers: they watch event
trajectories. CDP-synthesised events (Playwright's ``click``/``type``) are
trusted by the browser but arrive with machine-perfect timing and straight
paths. This module drives the browser with OS-level input on the X display
the headed browser runs under (Xvfb, see searx.network.browser):

- the pointer travels a quadratic Bezier curve with a random control point
  and per-step jitter, emitted as real XTEST events via pyautogui
- buttons are clicked after a human-scale hover pause and press duration
- text is typed with human cadence: uneven keys, short fast bursts, and the
  occasional mid-word hesitation

Coordinate spaces: Playwright boxes are page (viewport) coordinates while
pyautogui needs screen coordinates. The mapping reads the window origin
from JS (``window.screenX/screenY``, outer vs inner sizes), which is exact
per window including the browser chrome.

There is one X pointer per display, so a process-wide asyncio lock
(:py:func:`human_session`) serialises human sessions across pool lanes.
"""

from __future__ import annotations

__all__ = [
    "HumanInputError",
    "human_session",
    "human_search_on_page",
    "human_read_results",
    "human_solve_challenge",
]

import asyncio
import os
import random
import sys
import time
from contextlib import asynccontextmanager

from searx import logger

logger = logger.getChild('network.human_input')

# Candidate selectors for a provider's search box, most specific first.
_SEARCH_INPUT_SELECTORS = (
    "textarea[name='q']",
    "input[name='q']",
    "input[name='p']",
    "input[name='query']",
    "input[name='text']",
    "input[name='s']",
    "input[name='search']",
    "input[type='search']",
    "input[aria-label*='search' i]",
    "textarea[aria-label*='search' i]",
    "input[type='text']",
)

# Candidate selectors for the search button next to that box.
_SEARCH_BUTTON_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "input[name='btnK']",
    "button[name='btnG']",
    "#sb_form_go",
    "form button:not([type='button']):not([type='reset'])",
)

# Challenge checkboxes rendered inside cross-origin iframes. For reCAPTCHA
# only the *anchor* frame carries the checkbox: a bare src*=recaptcha also
# matches the bframe (image grid), and a frame_locator resolving to two
# frames fails strict mode.
_CHALLENGE_FRAME_SELECTORS = (
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[src*='hcaptcha.com']",
    "iframe[src*='recaptcha/enterprise/anchor']",
    "iframe[src*='recaptcha/api2/anchor']",
)
_CHALLENGE_FRAME_SELECTOR = ", ".join(_CHALLENGE_FRAME_SELECTORS)
_CHALLENGE_BOX_SELECTORS = (
    "input[type='checkbox']",
    "#checkbox",
    ".check",
    ".recaptcha-checkbox-border",
    "[role='checkbox']",
)

# Markers that the CURRENT page is a challenge interstitial: only then is it
# worth waiting the full settle window for the challenge iframe to mount
# (google's /sorry mounts the reCAPTCHA anchor a couple of seconds after the
# redirect; a plain page never grows one).
_CHALLENGE_PAGE_MARKERS = (
    "/sorry",
    "unusual traffic",
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "cf-chl",
)

_HUMAN_LOCK = asyncio.Lock()
_pyautogui = None


class HumanInputError(Exception):
    """Raised when OS-level input cannot be delivered to the browser."""


def _get_pyautogui():
    """Return the pyautogui module bound to the browser's X display.

    Imported lazily: importing before the display exists fails hard, and
    the module is expensive to import. On X errors the cache is dropped
    (:py:func:`_reset_pyautogui`) so the next call rebinds to the display.
    """
    global _pyautogui  # pylint: disable=global-statement
    if _pyautogui is not None:
        return _pyautogui
    from searx.network.browser import ensure_display

    display = ensure_display()
    if display is None:
        raise HumanInputError('no X display for human input (Xvfb unavailable)')
    os.environ['DISPLAY'] = display
    try:
        import pyautogui
    except Exception as e:
        raise HumanInputError(f'pyautogui unavailable: {e}') from e
    # The pool owns the pointer; a jittered move must never hit a corner
    # and raise FailSafeException.
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
    _pyautogui = pyautogui
    return _pyautogui


def _reset_pyautogui():
    """Drop the cached pyautogui (stale after an Xvfb restart)."""
    global _pyautogui  # pylint: disable=global-statement
    _pyautogui = None
    for name in list(sys.modules):
        if name == 'pyautogui' or name.startswith(('pymouse', 'mouseinfo', 'pyautogui.')):
            del sys.modules[name]


def _screen_size() -> tuple[int, int]:
    pyautogui = _get_pyautogui()
    size = pyautogui.size()
    return int(size.width), int(size.height)


def _clamp(x: float, y: float) -> tuple[int, int]:
    width, height = _screen_size()
    return int(max(0, min(x, width - 1))), int(max(0, min(y, height - 1)))


def quadratic_bezier(
    start: tuple[int, int], control: tuple[int, int], end: tuple[int, int], t: float
) -> tuple[float, float]:
    """Point on a quadratic Bezier curve at parameter ``t`` in [0, 1]."""
    u = 1.0 - t
    x = u * u * start[0] + 2 * u * t * control[0] + t * t * end[0]
    y = u * u * start[1] + 2 * u * t * control[1] + t * t * end[1]
    return x, y


async def human_like_real_mouse_move(
    start: tuple[int, int], end: tuple[int, int], steps: int = 0
) -> tuple[int, int]:
    """Move the real X pointer from ``start`` to ``end`` like a human hand.

    Quadratic Bezier path through a random control point, 30..200 steps,
    per-step jitter, 1-5ms between steps (the await lets the event loop
    breathe while the pointer moves).
    """
    pyautogui = _get_pyautogui()
    start_ts = time.monotonic()
    if not steps:
        steps = random.randint(30, 200)  # noqa: S311
    # Control point with a slight offset to create a curve.
    mid_x = (start[0] + end[0]) / 2 + random.randint(-400, 400)  # noqa: S311
    mid_y = (start[1] + end[1]) / 2 + random.randint(-200, 200)  # noqa: S311
    control = (mid_x, mid_y)
    final_x, final_y = end
    for i in range(steps + 1):
        t = i / steps
        x, y = quadratic_bezier(start, control, end, t)
        # Add some jitter for realism.
        final_x, final_y = _clamp(x + random.uniform(-1, 1), y + random.uniform(-1, 1))  # noqa: S311
        pyautogui.moveTo(final_x, final_y, _pause=False)
        await asyncio.sleep(random.uniform(0.001, 0.005))  # noqa: S311
    end_ts = time.monotonic()
    logger.debug('Took %f seconds for %d steps', end_ts - start_ts, steps)
    return final_x, final_y


def reset_input():
    """Drop the cached pyautogui binding.

    Called when a human session failed: the usual cause is a stale X
    connection (Xvfb was restarted), and the next session must rebind to
    the display instead of failing forever on the cached module.
    """
    _reset_pyautogui()


async def _human_click(x: int, y: int) -> None:
    """Bezier-move the pointer to (x, y), hover, then press and release."""
    pyautogui = _get_pyautogui()
    start = pyautogui.position()
    start = (int(start.x), int(start.y))
    if start != (x, y):
        await human_like_real_mouse_move(start, (x, y))
    await asyncio.sleep(random.uniform(0.08, 0.3))  # noqa: S311 -- hover
    pyautogui.mouseDown(_pause=False)
    await asyncio.sleep(random.uniform(0.04, 0.12))  # noqa: S311 -- press
    pyautogui.mouseUp(_pause=False)


async def _human_type(page, text: str) -> None:
    """Type ``text`` on the real keyboard with human cadence.

    Fast typists alternate short bursts of quick keys with slower keys and
    the occasional mid-word hesitation; a longer pause happens between
    sentences or when hunting the next key.

    The X keymap carries only basic Latin: a character outside it is sent
    as a single page-level key event instead of aborting the session (the
    page sees a normal trusted key event either way).
    """
    pyautogui = _get_pyautogui()
    burst = 0
    for ch in text:
        try:
            pyautogui.write(ch, _pause=False)
        except Exception:  # pylint: disable=broad-except
            logger.debug('human search: %r not on the X keymap, page-level key', ch)
            await page.keyboard.type(ch, delay=0)
        if burst > 0:
            # inside a burst: quick, slightly uneven keys
            delay = random.uniform(0.04, 0.09)  # noqa: S311
            burst -= 1
        elif random.random() < 0.1:  # noqa: S311
            # hesitate: think about the next word, hunt the next key
            delay = random.uniform(0.2, 0.7)  # noqa: S311
        else:
            delay = random.uniform(0.06, 0.18)  # noqa: S311
            if random.random() < 0.25:  # noqa: S311
                # start a short burst of fast keys
                burst = random.randint(2, 5)  # noqa: S311
        await asyncio.sleep(delay)
        if ch == ' ' and random.random() < 0.12:  # noqa: S311
            await asyncio.sleep(random.uniform(0.15, 0.45))  # noqa: S311 -- pause


async def _to_screen(page, x: float, y: float) -> tuple[int, int]:
    """Map page (viewport) coordinates to screen coordinates.

    Reads the window's screen origin and chrome size from JS, so the math
    holds for every pool window regardless of where Chromium placed it.
    """
    geo = await page.evaluate(
        "() => ({sx: window.screenX, sy: window.screenY,"
        " ow: window.outerWidth, oh: window.outerHeight,"
        " iw: window.innerWidth, ih: window.innerHeight})"
    )
    vx = geo['sx'] + (geo['ow'] - geo['iw']) // 2
    vy = geo['sy'] + (geo['oh'] - geo['ih'])
    return _clamp(vx + x, vy + y)


async def _human_click_locator(locator) -> bool:
    """Click a Playwright locator with the real mouse. False if invisible."""
    try:
        box = await locator.bounding_box()
    except Exception:  # pylint: disable=broad-except
        return False
    if not box or box['width'] <= 1 or box['height'] <= 1:
        return False
    x, y = await _to_screen(
        locator.page,
        box['x'] + box['width'] / 2 + random.uniform(-2, 2),  # noqa: S311
        box['y'] + box['height'] / 2 + random.uniform(-2, 2),  # noqa: S311
    )
    await _human_click(x, y)
    return True


async def _find_visible(page, selectors: tuple[str, ...]):
    """Return the first visible locator matching any selector, else None."""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible():
                return locator
        except Exception:  # pylint: disable=broad-except
            continue
    return None


@asynccontextmanager
async def human_session():
    """Serialize human input across the browser pool (one X pointer)."""
    async with _HUMAN_LOCK:
        yield


async def human_search_on_page(page, query: str) -> bool:
    """Run a search on an already-open provider page like a human would.

    Scans the page for a moment, clicks the search box, types the query,
    then clicks the search button (Enter as fallback). Returns False when
    the page has no visible search box.
    """
    pyautogui = _get_pyautogui()
    await page.bring_to_front()
    # orient: scan the page before reaching for the search box
    await asyncio.sleep(random.uniform(0.4, 1.3))  # noqa: S311
    input_loc = await _find_visible(page, _SEARCH_INPUT_SELECTORS)
    if input_loc is None:
        logger.debug('human search: no search box found on %s', page.url)
        return False
    try:
        await input_loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:  # pylint: disable=broad-except
        pass

    if not await _human_click_locator(input_loc):
        return False

    await _human_type(page, query)
    # proofread what was typed before firing the search
    await asyncio.sleep(random.uniform(0.4, 1.4))  # noqa: S311

    button_loc = await _find_visible(page, _SEARCH_BUTTON_SELECTORS)
    if button_loc is not None:
        await _human_click_locator(button_loc)
    else:
        pyautogui.press('enter', _pause=False)
    return True


async def human_read_results(page) -> None:
    """Behave like a human scanning a fresh results page.

    Dwell on the page, give the results list a small scroll with the real
    mouse wheel, then settle. This is also the window in which the page
    finishes loading lazy content before the DOM is captured.
    """
    pyautogui = _get_pyautogui()
    await asyncio.sleep(random.uniform(1.2, 2.8))  # noqa: S311 -- first look
    pyautogui.scroll(-random.randint(2, 4))  # noqa: S311 -- scan down a bit
    await asyncio.sleep(random.uniform(0.5, 1.5))  # noqa: S311 -- settle


async def human_solve_challenge(page, *, settle_ms: int = 6000) -> bool:
    """Try to solve a challenge interstitial by clicking its checkbox.

    Looks into the known challenge iframes (Cloudflare Turnstile, hCaptcha,
    reCAPTCHA) for a checkbox-like element and clicks it with the real
    mouse, then waits for the challenge to clear. Returns False when no
    checkbox was found.

    The challenge iframe mounts a moment AFTER the interstitial page loads,
    so when the page looks like a challenge the frame wait uses the full
    settle window; on a plain page the quick check keeps the search flowing.
    """
    await page.bring_to_front()
    challenge_expected = await _page_looks_like_challenge(page)
    try:
        await page.wait_for_selector(
            _CHALLENGE_FRAME_SELECTOR,
            timeout=settle_ms if challenge_expected else 800,
            state='attached',
        )
    except Exception:  # pylint: disable=broad-except
        return False
    for frame_selector in _CHALLENGE_FRAME_SELECTORS:
        frame_loc = page.frame_locator(frame_selector)
        for box_selector in _CHALLENGE_BOX_SELECTORS:
            locator = frame_loc.locator(box_selector).first
            try:
                if await locator.count() == 0:
                    continue
            except Exception:  # pylint: disable=broad-except
                continue
            logger.info('human input: clicking challenge checkbox (%s)', frame_selector)
            if await _human_click_locator(locator):
                try:
                    await page.wait_for_load_state('networkidle', timeout=settle_ms)
                except Exception:  # pylint: disable=broad-except
                    pass
                return True
    return False


async def _page_looks_like_challenge(page) -> bool:
    """Heuristic: is the page showing a challenge / rate-limit interstitial?"""
    markers = _CHALLENGE_PAGE_MARKERS
    url = (page.url or '').lower()
    if any(marker in url for marker in markers):
        return True
    try:
        body = (await page.content())[:8192].lower()
    except Exception:  # pylint: disable=broad-except
        return False
    return any(marker in body for marker in markers)
