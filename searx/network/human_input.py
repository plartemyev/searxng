# SPDX-License-Identifier: AGPL-3.0-or-later
"""Human-like input on the X display for the browser fetch pool.

Bot detectors score more than TLS and headers: they watch event
trajectories. CDP-synthesised events (Playwright's ``click``/``type``) are
trusted by the browser but arrive with machine-perfect timing and straight
paths. This module drives the browser with OS-level input on the X display
the headed browser runs under (Xvfb, see searx.network.browser):

- the pointer travels a quadratic Bezier curve with a random control point
  and per-step jitter, emitted as real X input (pointer warps and XTEST
  events on the lane's own display)
- buttons are clicked after a human-scale hover pause and press duration
- text is typed with human cadence: uneven keys, short fast bursts, and the
  occasional mid-word hesitation
- lingering is not stillness: pauses and result reading keep the hand
  making small idle drifts instead of freezing the pointer

Coordinate spaces: Playwright boxes are page (viewport) coordinates while
the input layer needs screen coordinates. The mapping reads the window
origin from JS (``window.screenX/screenY``, outer vs inner sizes), which is
exact per window including the browser chrome.

Every pool lane runs its browser on its own X display, and each session
connects to exactly its lane's display: the pointers are independent, so
sessions on different lanes run concurrently -- there is no process-wide
lock, and a lane's simulated click can never land in another lane's window.
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
import random
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


class HumanInputError(Exception):
    """Raised when OS-level input cannot be delivered to the browser."""


class _XPointer:
    """OS-level pointer and keyboard on ONE explicit X display.

    Replaces pyautogui, whose X connection is process-global: it binds to
    ``$DISPLAY`` at import time, so one process can drive exactly one
    display. Pool lanes run one browser each on its own display and need
    independent, concurrent input channels. python-xlib connects to an
    explicit display string, so every session owns its connection and no
    process state is mutated.

    Clicks and keys go through the XTEST extension (the same fake-input
    path pyautogui and xdotool use); pointer moves are warps on the
    session's own display.
    """

    _SHIFT_KEYSYM = 0xFFE1  # XK_Shift_L

    # named keys the flows use; keysyms are Latin-1 code points otherwise
    _NAMED_KEYSYMS = {
        'enter': 0xFF0D,
        'return': 0xFF0D,
        'tab': 0xFF09,
        'esc': 0xFF1B,
        'escape': 0xFF1B,
    }

    def __init__(self, display: str):
        try:
            from Xlib import X  # pylint: disable=import-outside-toplevel
            from Xlib.ext import xtest  # pylint: disable=import-outside-toplevel
            from Xlib.display import Display  # pylint: disable=import-outside-toplevel
        except ImportError as e:
            raise HumanInputError(f'python-xlib unavailable: {e}') from e
        self._X = X
        self._xtest = xtest
        try:
            self._display = Display(display)
            self._root = self._display.screen().root
            self._display.sync()
        except Exception as e:
            raise HumanInputError(
                f'cannot connect to X display {display}: {e}'
            ) from e
        self._keymap = self._build_keymap()

    def _build_keymap(self) -> dict[int, tuple[int, bool]]:
        """Map keysym -> (keycode, shift required), from the server keymap.

        A keycode carries an unshifted and a shifted keysym (columns 1 and
        2); the first keycode claiming a keysym wins.
        """
        info = self._display.display.info
        rows = self._display.get_keyboard_mapping(
            info.min_keycode, info.max_keycode - info.min_keycode + 1
        )
        keymap: dict[int, tuple[int, bool]] = {}
        for offset, syms in enumerate(rows):
            keycode = info.min_keycode + offset
            for column, keysym in enumerate(syms):
                if keysym and keysym not in keymap:
                    keymap[keysym] = (keycode, column == 1)
        return keymap

    # -- pointer ---------------------------------------------------------

    def size(self) -> tuple[int, int]:
        geometry = self._root.get_geometry()
        return int(geometry.width), int(geometry.height)

    def position(self) -> tuple[int, int]:
        pointer = self._root.query_pointer()
        return int(pointer.root_x), int(pointer.root_y)

    def move_to(self, x: int, y: int) -> None:
        self._root.warp_pointer(int(x), int(y))
        self._display.sync()

    def mouse_down(self) -> None:
        self._xtest.fake_input(self._display, self._X.ButtonPress, 1)
        self._display.sync()

    def mouse_up(self) -> None:
        self._xtest.fake_input(self._display, self._X.ButtonRelease, 1)
        self._display.sync()

    def scroll(self, clicks: int) -> None:
        """Scroll ``clicks`` wheel notches (pyautogui sign: negative is down)."""
        button = self._X.Button4 if clicks > 0 else self._X.Button5
        for _ in range(abs(int(clicks))):
            self._xtest.fake_input(self._display, self._X.ButtonPress, button)
            self._xtest.fake_input(self._display, self._X.ButtonRelease, button)
            self._display.sync()

    # -- keyboard ----------------------------------------------------------

    def _key_entry(self, ch: str) -> tuple[int, bool] | None:
        """Keymap entry for a character, or None when not typable.

        X keysyms for Latin-1 printable characters equal their Unicode code
        points (XK_a == 0x61, XK_exclam == 0x21), so ``ord`` is the keysym;
        anything outside the range needs the server's keysym database and
        is left to the page-level fallback.
        """
        if len(ch) != 1:
            return None
        code = ord(ch)
        if not 0x20 <= code <= 0xFF:
            return None
        return self._keymap.get(code)

    def write(self, ch: str) -> None:
        """Type one character; HumanInputError when it is not on the keymap."""
        entry = self._key_entry(ch)
        if entry is None:
            raise HumanInputError(f'{ch!r} is not on the X keymap')
        keycode, shift = entry
        self._press_keycode(keycode, shift=shift)

    def press(self, name: str) -> None:
        """Press a named key (e.g. 'enter')."""
        keysym = self._NAMED_KEYSYMS.get(name.lower())
        entry = self._keymap.get(keysym) if keysym else None
        if entry is None:
            raise HumanInputError(f'key {name!r} is not on the X keymap')
        self._press_keycode(entry[0])

    def _press_keycode(self, keycode: int, *, shift: bool = False) -> None:
        shift_keycode = self._keymap.get(self._SHIFT_KEYSYM, (None, False))[0]
        if shift and shift_keycode is None:
            raise HumanInputError('no Shift key on the X keymap')
        if shift:
            self._xtest.fake_input(self._display, self._X.KeyPress, shift_keycode)
        self._xtest.fake_input(self._display, self._X.KeyPress, keycode)
        self._xtest.fake_input(self._display, self._X.KeyRelease, keycode)
        if shift:
            self._xtest.fake_input(self._display, self._X.KeyRelease, shift_keycode)
        self._display.sync()


def _clamp(pointer: _XPointer, x: float, y: float) -> tuple[int, int]:
    width, height = pointer.size()
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
    pointer: _XPointer,
    start: tuple[int, int],
    end: tuple[int, int],
    steps: int = 0,
) -> tuple[int, int]:
    """Move the real X pointer from ``start`` to ``end`` like a human hand.

    Quadratic Bezier path through a random control point, 30..200 steps,
    per-step jitter, 1-5ms between steps (the await lets the event loop
    breathe while the pointer moves).
    """
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
        final_x, final_y = _clamp(
            pointer, x + random.uniform(-1, 1), y + random.uniform(-1, 1)  # noqa: S311
        )
        pointer.move_to(final_x, final_y)
        await asyncio.sleep(random.uniform(0.001, 0.005))  # noqa: S311
    end_ts = time.monotonic()
    logger.debug('Took %f seconds for %d steps', end_ts - start_ts, steps)
    return final_x, final_y


async def _human_idle(
    pointer: _XPointer,
    seconds: float,
    *,
    radius_x: int = 90,
    radius_y: int = 60,
) -> None:
    """Linger like a human: idle micro-movements instead of a frozen pointer.

    Real hands keep making small corrections while the eyes read; a
    pointer that freezes for whole seconds during an active page is its
    own tell. Splits the pause into short Bezier drifts around the
    current position with still gaps between them.
    """
    anchor = pointer.position()
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            return
        if random.random() < 0.35:  # noqa: S311 -- stillness is human too
            await asyncio.sleep(min(remaining, random.uniform(0.15, 0.5)))  # noqa: S311
            continue
        target = (
            anchor[0] + random.randint(-radius_x, radius_x),  # noqa: S311
            anchor[1] + random.randint(-radius_y, radius_y),  # noqa: S311
        )
        steps = random.randint(8, 25)  # noqa: S311 -- short drift
        await human_like_real_mouse_move(pointer, anchor, target, steps=steps)


async def _human_click(pointer: _XPointer, x: int, y: int) -> None:
    """Bezier-move the pointer to (x, y), hover, then press and release."""
    start = pointer.position()
    if start != (x, y):
        await human_like_real_mouse_move(pointer, start, (x, y))
    await asyncio.sleep(random.uniform(0.08, 0.3))  # noqa: S311 -- hover
    pointer.mouse_down()
    await asyncio.sleep(random.uniform(0.04, 0.12))  # noqa: S311 -- press
    pointer.mouse_up()


async def _human_type(page, text: str, pointer: _XPointer) -> None:
    """Type ``text`` on the real keyboard with human cadence.

    Fast typists alternate short bursts of quick keys with slower keys and
    the occasional mid-word hesitation; a longer pause happens between
    sentences or when hunting the next key.

    The X keymap carries only basic Latin: a character outside it is sent
    as a single page-level key event instead of aborting the session (the
    page sees a normal trusted key event either way).
    """
    burst = 0
    for ch in text:
        try:
            pointer.write(ch)
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


async def _to_screen(
    page, pointer: _XPointer, x: float, y: float
) -> tuple[int, int]:
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
    return _clamp(pointer, vx + x, vy + y)


async def _human_click_locator(locator, pointer: _XPointer) -> bool:
    """Click a Playwright locator with the real mouse. False if invisible."""
    try:
        box = await locator.bounding_box()
    except Exception:  # pylint: disable=broad-except
        return False
    if not box or box['width'] <= 1 or box['height'] <= 1:
        return False
    x, y = await _to_screen(
        locator.page,
        pointer,
        box['x'] + box['width'] / 2 + random.uniform(-2, 2),  # noqa: S311
        box['y'] + box['height'] / 2 + random.uniform(-2, 2),  # noqa: S311
    )
    await _human_click(pointer, x, y)
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
async def human_session(display: str | None):
    """Bind a human input session to the lane's X display, as ``pointer``.

    Every lane has its own display and pointer: sessions on different
    lanes run concurrently and cannot interfere, so there is no lock.
    Raises :py:class:`HumanInputError` when the lane is headless or its
    display refuses the connection.
    """
    if not display:
        raise HumanInputError('no X display for human input (headless lane)')
    yield _XPointer(display)


async def human_search_on_page(page, query: str, pointer: _XPointer) -> bool:
    """Run a search on an already-open provider page like a human would.

    Scans the page for a moment, clicks the search box, types the query,
    then clicks the search button (Enter as fallback). Returns False when
    the page has no visible search box.
    """
    await page.bring_to_front()
    # orient: scan the page, hand drifting near the mouse, before reaching
    # for the search box
    await _human_idle(pointer, random.uniform(0.4, 1.3))  # noqa: S311
    input_loc = await _find_visible(page, _SEARCH_INPUT_SELECTORS)
    if input_loc is None:
        logger.debug('human search: no search box found on %s', page.url)
        return False
    try:
        await input_loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:  # pylint: disable=broad-except
        pass

    if not await _human_click_locator(input_loc, pointer):
        return False

    await _human_type(page, query, pointer)
    # proofread what was typed before firing the search; the hand rests
    # near the box, so the drift stays tight
    await _human_idle(
        pointer, random.uniform(0.4, 1.4), radius_x=40, radius_y=25  # noqa: S311
    )

    button_loc = await _find_visible(page, _SEARCH_BUTTON_SELECTORS)
    if button_loc is not None:
        await _human_click_locator(button_loc, pointer)
    else:
        pointer.press('enter')
    return True


async def human_read_results(page, pointer: _XPointer) -> None:
    """Behave like a human scanning a fresh results page.

    Dwell on the page with idle hand drift, give the results list a small
    scroll with the real mouse wheel while the pointer follows the content
    downward, then settle. This is also the window in which the page
    finishes loading lazy content before the DOM is captured.
    """
    await _human_idle(pointer, random.uniform(1.2, 2.8))  # noqa: S311 -- first look
    pointer.scroll(-random.randint(2, 4))  # noqa: S311 -- scan down a bit
    pos_x, pos_y = pointer.position()
    follow_y = pos_y + random.randint(60, 160)  # noqa: S311 -- eyes follow
    await human_like_real_mouse_move(
        pointer, (pos_x, pos_y), (pos_x, follow_y), steps=random.randint(10, 30)  # noqa: S311
    )
    await _human_idle(pointer, random.uniform(0.5, 1.5))  # noqa: S311 -- settle


async def human_solve_challenge(
    page, pointer: _XPointer, *, settle_ms: int = 6000
) -> bool:
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
            if await _human_click_locator(locator, pointer):
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
