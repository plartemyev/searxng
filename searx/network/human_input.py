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
    "human_solve_image_challenge",
    "human_clear_challenge",
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
    # The empty-box warning is an operational signal (clicks landing wrong);
    # the typed value itself only matters when tracing is on.
    try:
        typed = await input_loc.evaluate("el => el.value")
        if typed:
            if _TRACE_STATE["enabled"]:
                logger.debug("human search: typed %r", typed)
        else:
            logger.warning(
                "human search: the search box stayed empty on %s", page.url
            )
    except Exception:  # pylint: disable=broad-except
        pass
    # proofread what was typed before firing the search; the hand rests
    # near the box, so the drift stays tight
    await _human_idle(
        pointer, random.uniform(0.4, 1.4), radius_x=40, radius_y=25  # noqa: S311
    )

    button_loc = await _find_visible(page, _SEARCH_BUTTON_SELECTORS)
    if button_loc is not None:
        try:
            state = await button_loc.evaluate(
                "el => ({disabled: el.disabled, name: el.name || ''})"
            )
            if _TRACE_STATE["enabled"]:
                logger.debug("human search: submit control %s", state)
        except Exception:  # pylint: disable=broad-except
            pass
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


# --------------------------------------------------------------------------
# Image-grid challenges (reCAPTCHA image select, hCaptcha task grid)
#
# When a checkbox click escalates to an image grid, the vision solver
# (searx.network.captcha_vision) decides which tiles match the instruction.
# Only the DECISION comes from the model: every click is a real mouse event
# on the lane's X display, at human pace, exactly like the checkbox flow.
# --------------------------------------------------------------------------

class _ImageGridProfile:
    """Selectors describing one provider's image-grid challenge DOM."""

    def __init__(
        self,
        name: str,
        frame_selectors: tuple[str, ...],
        ready_selector: str,
        instruction_selector: str,
        tile_selector: str,
        tile_img_selector: str | None,
        verify_selectors: tuple[str, ...],
        skip_selectors: tuple[str, ...],
    ):
        self.name = name
        self.frame_selectors = frame_selectors
        self.ready_selector = ready_selector
        self.instruction_selector = instruction_selector
        self.tile_selector = tile_selector
        self.tile_img_selector = tile_img_selector
        self.verify_selectors = verify_selectors
        self.skip_selectors = skip_selectors


_IMAGE_GRID_PROFILES = (
    _ImageGridProfile(
        name='recaptcha',
        # bframe carries the image challenge; anchor frames match too
        # broadly and never hold tiles
        frame_selectors=(
            "iframe[src*='recaptcha/api2/bframe']",
            "iframe[src*='recaptcha/enterprise/bframe']",
        ),
        ready_selector='.rc-imageselect-instructions',
        instruction_selector='.rc-imageselect-desc-wrapper',
        tile_selector='.rc-imageselect-tile',
        # the challenge photos are <img> elements streamed in per tile
        tile_img_selector="img[class*='rc-image-tile']",
        verify_selectors=('#recaptcha-verify-button',),
        # the verify button relabels itself (Verify / Skip in the UI
        # language); an empty selection pressed on it acts as the skip
        skip_selectors=(),
    ),
    _ImageGridProfile(
        name='hcaptcha',
        frame_selectors=("iframe[src*='hcaptcha.com']",),
        ready_selector='.challenge-view .task-image',
        instruction_selector='.prompt-text',
        tile_selector='.task-image',
        # hCaptcha paints its photos as background images, no <img> to poll
        tile_img_selector=None,
        verify_selectors=('.challenge-button', '.button-submit'),
        skip_selectors=('button:has-text("Skip")', '.challenge-button-text'),
    ),
)

# a mounted grid is waited for longer only when the page already smells like
# a challenge; on a plain page the quick probe keeps searches flowing
_IMAGE_GRID_QUICK_MS = 800
_IMAGE_GRID_FULL_MS = 7000

# reCAPTCHA grid sizes: 3x3, 4x4 and the 2x2 "select one region" variant
_GRID_SHAPES = {4: (2, 2), 9: (3, 3), 16: (4, 4)}


def _grid_shape(tile_count: int) -> tuple[int, int]:
    """(rows, cols) for a tile count, best effort for unknown layouts."""
    return _GRID_SHAPES.get(tile_count, (tile_count, 1))


async def _wait_for_image_grid(page, *, challenge_expected: bool):
    """Find a mounted, visible image grid. Returns (profile, frame) or None."""
    timeout_s = (_IMAGE_GRID_FULL_MS if challenge_expected else _IMAGE_GRID_QUICK_MS) / 1000
    deadline = time.monotonic() + timeout_s
    while True:
        for profile in _IMAGE_GRID_PROFILES:
            for frame_selector in profile.frame_selectors:
                frame_loc = page.frame_locator(frame_selector)
                ready = frame_loc.locator(profile.ready_selector).first
                try:
                    if await ready.is_visible():
                        return profile, frame_loc
                except Exception:  # pylint: disable=broad-except
                    continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)


async def _frame_text(frame_loc, selector: str) -> str:
    try:
        text = await frame_loc.locator(selector).first.inner_text()
    except Exception:  # pylint: disable=broad-except
        return ''
    return ' '.join((text or '').split())


async def _frame_html(frame_loc) -> str:
    try:
        return await frame_loc.locator('body').evaluate('el => el.outerHTML')
    except Exception:  # pylint: disable=broad-except
        return ''


async def _element_count(locator) -> int:
    try:
        return await locator.count()
    except Exception:  # pylint: disable=broad-except
        return 0


async def _stable_tile_count(tiles) -> int:
    """Tile count once it stops changing: dynamic grids stream tiles in,
    and a count read too early solves the wrong grid. Returns the last
    count seen when the grid does not settle in time."""
    count = 0
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        current = await _element_count(tiles)
        if current > 0 and current == count:
            return count
        count = current
        await asyncio.sleep(0.3)
    return count


async def _wait_tiles_painted(frame_loc, profile: _ImageGridProfile, tile_count: int) -> None:
    """Give the challenge photos a moment to render before the screenshot.

    reCAPTCHA streams each tile's image separately; a screenshot taken too
    early captures transparent tiles (the model then sees the page behind
    the widget and answers nonsense). hCaptcha has no <img> to poll.
    """
    if not profile.tile_img_selector:
        return
    img = frame_loc.locator(profile.tile_img_selector)
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        try:
            flags = []
            for i in range(min(tile_count, 25)):
                flags.append(
                    await img.nth(i).evaluate('el => el.complete && el.naturalWidth > 0')
                )
            if flags and all(flags):
                return
        except Exception:  # pylint: disable=broad-except
            return  # DOM shape differs from the expectation: don't wait
        await asyncio.sleep(0.25)


async def _iframe_screenshot(page, profile: _ImageGridProfile) -> bytes | None:
    """Screenshot the challenge widget.

    The screenshot targets the IFRAME element in the parent page, not an
    element inside the frame: a cross-frame element screenshot of the grid
    table rendered parent-page content through unpainted tiles, while the
    iframe element (a plain parent-frame element) captures exactly the
    rendered widget, instruction text included.
    """
    for frame_selector in profile.frame_selectors:
        iframe_loc = page.locator(frame_selector).first
        try:
            if not await iframe_loc.is_visible():
                continue
            png = await iframe_loc.screenshot()
            if png:
                return png
        except Exception:  # pylint: disable=broad-except
            continue
    return None


async def _tile_rects(frame_loc, profile: _ImageGridProfile, tile_count: int) -> list[list[int]] | None:
    """The tiles' rectangles in iframe-viewport coordinates (the same space
    as the widget screenshot), DOM order = row-major. None on any failure."""
    tiles = frame_loc.locator(profile.tile_selector)
    rects: list[list[int]] = []
    for i in range(min(tile_count, 25)):
        try:
            rect = await tiles.nth(i).evaluate(
                'el => (r => [r.x, r.y, r.width, r.height])(el.getBoundingClientRect())'
            )
        except Exception:  # pylint: disable=broad-except
            return None
        if not isinstance(rect, list) or len(rect) != 4 or rect[2] <= 1 or rect[3] <= 1:
            return None
        rects.append([int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])])
    return rects


def _tiles_fully_painted(png: bytes, rects: list[list[int]]) -> bool:
    """False when any tile region is a blank placeholder.

    Dynamic challenges sometimes stream one tile late: the DOM paint flags
    read complete while the pixels are still a flat white block. The
    standard deviation of the tile's luminance separates a photo (or any
    real image content) from a blank block.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from searx.network.captcha_vision import crop_png, _png_decode

        for x, y, w, h in rects:
            tile = crop_png(png, x, y, w, h)
            _, _, _, pixels = _png_decode(tile)
            n = len(pixels) // 3
            if n == 0:
                return False
            mean = sum(pixels[0::3]) / n
            variance = sum((v - mean) ** 2 for v in pixels[0::3]) / n
            if variance ** 0.5 < 10.0:
                return False
        return True
    except Exception:  # pylint: disable=broad-except
        return True  # codec trouble must not wedge the round loop


async def _click_first_visible(frame_loc, selectors: tuple[str, ...], pointer) -> bool:
    """Click the first visible button among ``selectors`` with the real mouse."""
    for selector in selectors:
        locator = frame_loc.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
        except Exception:  # pylint: disable=broad-except
            continue
        if await _human_click_locator(locator, pointer):
            return True
    return False


# TEMPORARY (2026-09-26): dump what the vision model actually receives, for
# diagnosing wrong tile answers. Gated by outgoing.browser_debug_trace.
# Diagnostic tracing (DOM captures, typed-value logs) is off by default.
# Enable with outgoing.browser_debug_trace in the settings; the dumps then
# land in /tmp/captcha_debug (the directory must exist as well).
_TRACE_STATE = {"enabled": False}


def set_debug_trace(enabled: bool) -> None:
    """Switch diagnostic tracing on or off (settings-driven)."""
    _TRACE_STATE["enabled"] = bool(enabled)


def debug_trace_enabled() -> bool:
    return _TRACE_STATE["enabled"]


def _debug_dump(profile_name: str, round_index: int, png: bytes, instruction: str, html: str = '') -> None:
    import os

    if not _TRACE_STATE["enabled"]:
        return
    dump_dir = '/tmp/captcha_debug'
    if not os.path.isdir(dump_dir) or not png:
        return
    try:
        stamp = time.strftime('%H%M%S')
        with open(
            os.path.join(dump_dir, f'{profile_name}_r{round_index}_{stamp}.png'), 'wb'
        ) as handle:
            handle.write(png)
        with open(
            os.path.join(dump_dir, f'{profile_name}_r{round_index}_{stamp}.txt'), 'w', encoding='utf-8'
        ) as handle:
            handle.write(instruction)
        if html:
            with open(
                os.path.join(dump_dir, f'{profile_name}_r{round_index}_{stamp}.html'), 'w', encoding='utf-8'
            ) as handle:
                handle.write(html)
    except OSError:
        pass


async def _run_image_rounds(page, pointer, solver, max_rounds: int, found) -> bool:
    """Solve a mounted image grid, one vision round per image set (slide).

    A slide is consumed only when VERIFY is pressed: a grid that grew
    (dynamically added tiles) while the model was thinking is re-read and
    re-solved on the same slide. Between rounds the pointer waits like a
    person reading the next instruction; success is the browser leaving
    the challenge page.
    """
    solved = False
    round_index = 1
    re_reads = 0
    fresh_slide = True
    while round_index <= max_rounds:
        if fresh_slide and round_index > 1:
            # a person reads the new image set before acting on it; the
            # next grid mounts after the verify round trip, so this probe
            # uses the full window -- a quick probe here would give up
            # before round two ever appears
            await asyncio.sleep(random.uniform(1.0, 2.2))  # noqa: S311
            try:
                found = await _wait_for_image_grid(page, challenge_expected=True)
            except Exception:  # pylint: disable=broad-except
                found = None
            if found is None:
                solved = not await _page_looks_like_challenge(page)
                break
        fresh_slide = True
        profile, frame_loc = found

        try:
            instruction = await _frame_text(frame_loc, profile.instruction_selector)
            tiles = frame_loc.locator(profile.tile_selector)
            tile_count = await _stable_tile_count(tiles)
            if tile_count <= 0:
                solved = not await _page_looks_like_challenge(page)
                break
            await _wait_tiles_painted(frame_loc, profile, tile_count)
            png = await _iframe_screenshot(page, profile)
            if png is None:
                solved = not await _page_looks_like_challenge(page)
                break
        except Exception:  # pylint: disable=broad-except
            # the frame can detach when the challenge clears mid-round
            solved = not await _page_looks_like_challenge(page)
            break

        rows, cols = _grid_shape(tile_count)
        logger.info(
            'human input: %s image challenge round %s/%s: %s tiles, asking %s',
            profile.name,
            round_index,
            max_rounds,
            tile_count,
            solver.describe(),
        )
        _debug_dump(profile.name, round_index, png, instruction, await _frame_html(frame_loc))
        # presentation is decided by the tiles' own geometry: uniform grids
        # of any dimension become row strips, irregular ones fall back to
        # per-cell images, and DOM failures to the whole widget screenshot
        grid_images = None
        rects = None
        paint_deadline = time.monotonic() + 8.0
        while True:
            try:
                fresh_count = await _element_count(tiles)
                if fresh_count > 0 and fresh_count != tile_count:
                    logger.info(
                        'human input: %s grid added tiles: %s -> %s',
                        profile.name,
                        tile_count,
                        fresh_count,
                    )
                    tile_count = fresh_count
                    await _wait_tiles_painted(frame_loc, profile, tile_count)
                    png = await _iframe_screenshot(page, profile) or png
                rects = await _tile_rects(frame_loc, profile, tile_count)
            except Exception:  # pylint: disable=broad-except
                rects = None
                break
            if rects and _tiles_fully_painted(png, rects):
                break
            if time.monotonic() >= paint_deadline:
                break
            # a late tile: reshoot and check the pixels again
            await asyncio.sleep(random.uniform(0.5, 0.9))  # noqa: S311
            png = await _iframe_screenshot(page, profile) or png
        if rects:
            try:
                # pylint: disable=import-outside-toplevel
                from searx.network.captcha_vision import build_row_strips, crop_cells, derive_grid_layout

                layout = derive_grid_layout(rects)
                if layout is not None:
                    rows, cols = layout
                    grid_images = ("rows", build_row_strips(png, rects, cols))
                else:
                    grid_images = ("cells", crop_cells(png, rects))
            except Exception:  # pylint: disable=broad-except
                grid_images = None
        try:
            # blocking HTTP stays off the lane's event loop
            solution = await asyncio.to_thread(
                solver.solve_grid, png, grid_images, instruction, tile_count, rows, cols
            )
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                'human input: vision solver failed on %s image challenge',
                profile.name,
                exc_info=True,
            )
            return False
        logger.info(
            'human input: vision says tiles=%s action=%s (instruction %r)',
            list(solution.tiles),
            solution.action,
            instruction,
        )

        # the challenge can add tiles while the model thinks: a stale
        # answer would click wrong tiles, so re-read the grid instead.
        # This costs no slide -- nothing was pressed yet.
        try:
            fresh_count = await _element_count(tiles)
        except Exception:  # pylint: disable=broad-except
            fresh_count = tile_count
        if fresh_count > 0 and fresh_count != tile_count and re_reads < 8:
            re_reads += 1
            fresh_slide = False
            logger.info(
                'human input: %s grid changed while solving: %s -> %s tiles;'
                ' re-reading the grid',
                profile.name,
                tile_count,
                fresh_count,
            )
            continue

        # look over the grid, then click the matching tiles like a person:
        # uneven gaps between clicks, an occasional longer double-take
        await asyncio.sleep(random.uniform(0.5, 1.2))  # noqa: S311
        clicked_tiles = 0
        for tile_index in solution.tiles:
            tile_loc = tiles.nth(tile_index)
            if await _human_click_locator(tile_loc, pointer):
                clicked_tiles += 1
            else:
                logger.warning('human input: tile %s not clickable', tile_index)
            if random.random() < 0.25:  # noqa: S311
                await asyncio.sleep(random.uniform(0.8, 1.8))  # noqa: S311
            else:
                await asyncio.sleep(random.uniform(0.35, 0.95))  # noqa: S311

        # pre-press screenshot: selected tiles show overlays and the press
        # clears them; post-press: the error banner shows on a reject
        if _TRACE_STATE["enabled"]:
            try:
                sel_png = await _iframe_screenshot(page, profile)
                if sel_png:
                    _debug_dump(profile.name + '_sel', round_index, sel_png, page.url)
            except Exception:  # pylint: disable=broad-except
                pass

        # hover the button a moment before pressing it
        await asyncio.sleep(random.uniform(0.6, 1.4))  # noqa: S311
        if solution.action == 'skip' and clicked_tiles == 0:
            clicked = await _click_first_visible(frame_loc, profile.skip_selectors, pointer)
            if not clicked:
                clicked = await _click_first_visible(frame_loc, profile.verify_selectors, pointer)
        else:
            clicked = await _click_first_visible(frame_loc, profile.verify_selectors, pointer)
        if not clicked:
            logger.warning('human input: no VERIFY/SKIP button found in %s challenge', profile.name)
            return False
        if _TRACE_STATE["enabled"]:
            try:
                post_png = await _iframe_screenshot(page, profile)
                if post_png:
                    _debug_dump(profile.name + '_post', round_index, post_png, page.url)
            except Exception:  # pylint: disable=broad-except
                pass
        logger.info('human input: pressed VERIFY for round %s, page %s', round_index, page.url)

        # the press consumes the slide; grid re-reads above are free
        round_index += 1
        re_reads = 0

        # wait out the round trip: the next image set replaces this one, or
        # the browser leaves the challenge page entirely
        await asyncio.sleep(random.uniform(2.0, 3.5))  # noqa: S311

    if not solved:
        logger.warning('human input: image challenge not solved after %s slide(s)', max_rounds)
    return solved


async def human_solve_image_challenge(page, pointer, *, solver=None, max_rounds: int | None = None) -> bool:
    """Solve an escalated image-grid challenge with vision assistance.

    Returns False immediately when no vision solver is configured or no grid
    is mounted; otherwise runs up to ``max_rounds`` vision rounds. Every
    interaction is real X input (see :py:func:`_run_image_rounds`); only
    screenshots and text reads are programmatic, which the provider cannot
    distinguish from accessibility tooling.
    """
    if solver is None:
        # pylint: disable=import-outside-toplevel
        from searx.network.captcha_vision import get_vision_solver

        solver = get_vision_solver()
    if solver is None:
        return False
    if max_rounds is None:
        max_rounds = max(1, solver.cfg.max_rounds)

    found = await _wait_for_image_grid(page, challenge_expected=False)
    if found is None:
        return False
    return await _run_image_rounds(page, pointer, solver, max_rounds, found)


async def human_clear_challenge(page, pointer, *, settle_ms: int = 6000) -> bool:
    """Clear a challenge interstitial, checkbox first, vision second.

    Wraps :py:func:`human_solve_challenge` (the checkbox click) and
    :py:func:`human_solve_image_challenge` (the grid escalation the click
    often triggers). Both are always evaluated: a clicked checkbox and a
    subsequently mounted grid belong to the same challenge.
    """
    clicked = await human_solve_challenge(page, pointer, settle_ms=settle_ms)
    vision_solved = await human_solve_image_challenge(page, pointer)
    return clicked or vision_solved
