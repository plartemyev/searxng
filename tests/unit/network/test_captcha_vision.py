# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the vision-assisted image-grid challenge solver."""

# pylint: disable=missing-module-docstring,missing-function-docstring
# pylint: disable=protected-access,unused-argument,redefined-outer-name

import asyncio
import base64
import socket
import struct
import zlib

import pytest
from curl_cffi.requests.exceptions import RequestException

from searx.network import captcha_vision, human_input
from searx.network.captcha_vision import (
    OpenAICompatVisionSolver,
    VisionSolverConfig,
    VisionSolverError,
    grid_prompt,
)

# --------------------------------------------------------------------------
# answer parsing


def test_parse_plain_json():
    solution = OpenAICompatVisionSolver.parse_solution('{"tiles": [1, 4], "action": "submit"}', 9)
    assert solution.tiles == (1, 4)
    assert solution.action == "submit"


def test_parse_strips_markdown_fence():
    raw = '```json\n{\n  "tiles": [0, 4, 8],\n  "action": "submit"\n}\n```'
    solution = OpenAICompatVisionSolver.parse_solution(raw, 9)
    assert solution.tiles == (0, 4, 8)


def test_parse_tolerates_prose_around_json():
    raw = 'The matching tiles are:\n{"tiles": [2], "action": "submit"}\nHope this helps!'
    assert OpenAICompatVisionSolver.parse_solution(raw, 9).tiles == (2,)


def test_parse_drops_out_of_range_and_duplicates():
    solution = OpenAICompatVisionSolver.parse_solution('{"tiles": [0, 9, 4, 4, -1], "action": "submit"}', 9)
    assert solution.tiles == (0, 4)


def test_parse_bare_integer_fallback():
    assert OpenAICompatVisionSolver.parse_solution("tiles 1 and 7 look right", 9).tiles == (1, 7)


def test_parse_action_skip():
    solution = OpenAICompatVisionSolver.parse_solution('{"tiles": [], "action": "skip"}', 9)
    assert solution.tiles == ()
    assert solution.action == "skip"


def test_parse_empty_submit_becomes_skip():
    # a submit with nothing selected would only burn a round
    solution = OpenAICompatVisionSolver.parse_solution('{"tiles": [], "action": "submit"}', 9)
    assert solution.action == "skip"


def test_parse_unusable_answer_raises():
    with pytest.raises(VisionSolverError):
        OpenAICompatVisionSolver.parse_solution("I cannot see any image.", 9)


def test_parse_ignores_digits_in_long_reasoning_prose():
    # a reasoning field with no conclusion: its digits are not answers
    prose = (
        "Let me analyze this image carefully. It's a 3x3 grid of colored squares. "
        "Row 0: Tile 0 (0,0) is red. Row 1: Tile 4 (1,1) is red. " * 3
    )
    with pytest.raises(VisionSolverError):
        OpenAICompatVisionSolver.parse_solution(prose, 9)


def test_parse_finds_json_in_reasoning_conclusion():
    reasoning = (
        "Row 0: tile 0 is red. Row 1: tile 4 is red. Row 2: tile 8 is red. "
        'So the answer is {"tiles": [0, 4, 8], "action": "submit"}'
    )
    assert OpenAICompatVisionSolver.parse_solution(reasoning, 9).tiles == (0, 4, 8)


def test_chat_vision_falls_back_to_reasoning_field(fake_transport):
    fake_transport.responses = [
        {"choices": [{"message": {"content": "", "reasoning": 'thinking... {"tiles": [3], "action": "submit"}'}}]}
    ]
    assert solver().chat_vision("p", b"img") == 'thinking... {"tiles": [3], "action": "submit"}'


def test_grid_prompt_carries_instruction_and_contract():
    prompt = grid_prompt("Select all squares with traffic lights", 9, 3, 3)
    assert "traffic lights" in prompt
    assert "9 tiles (3 rows x 3 columns)" in prompt
    assert '"action"' in prompt and '"tiles"' in prompt
    assert "strict JSON" in prompt


# --------------------------------------------------------------------------
# config


def test_from_settings_reads_all_keys(monkeypatch):
    values = {
        "outgoing.captcha_vision.endpoint": " http://172.17.172.35:11434 ",
        "outgoing.captcha_vision.model": " ornith-1.5:9b ",
        "outgoing.captcha_vision.api_key": "",
        "outgoing.captcha_vision.context_size": 58192,
        "outgoing.captcha_vision.timeout": 360,
        "outgoing.captcha_vision.max_rounds": 3,
        "outgoing.captcha_vision.temperature": 0.1,
        "outgoing.captcha_vision.max_tokens": 300,
    }
    monkeypatch.setattr(captcha_vision, "get_setting", lambda key, default=None: values.get(key, default))
    cfg = VisionSolverConfig.from_settings()
    assert cfg.endpoint == "http://172.17.172.35:11434"
    assert cfg.model == "ornith-1.5:9b"
    assert cfg.context_size == 58192
    assert cfg.timeout == 360.0
    assert cfg.max_rounds == 3
    assert cfg.temperature == 0.1
    assert cfg.max_tokens == 300


def test_get_vision_solver_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(captcha_vision, "get_setting", lambda key, default=None: default)
    assert captcha_vision.get_vision_solver() is None


def test_get_vision_solver_built_when_configured(monkeypatch):
    values = {"outgoing.captcha_vision.endpoint": "http://x", "outgoing.captcha_vision.model": "m"}
    monkeypatch.setattr(captcha_vision, "get_setting", lambda key, default=None: values.get(key, default))
    solver = captcha_vision.get_vision_solver()
    assert solver is not None
    assert solver.describe() == "m @ http://x"


# --------------------------------------------------------------------------
# transport (OpenAI-compatible chat completions)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RequestException("boom")

    def json(self):
        return self._payload


class FakeRequests:
    posts = []

    def __init__(self, responses=None):
        self.responses = list(responses or [])

    def post(self, url, json=None, headers=None, timeout=None):  # pylint: disable=redefined-builtin
        FakeRequests.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if not isinstance(outcome, FakeResponse):
            outcome = FakeResponse(outcome)
        return outcome


@pytest.fixture()
def fake_transport(monkeypatch):
    FakeRequests.posts = []
    fake = FakeRequests([completion('{"tiles": [0], "action": "submit"}')])
    monkeypatch.setattr(captcha_vision, "curl_requests", fake)
    return fake


def completion(text):
    return {"choices": [{"message": {"content": text}}]}


def solver(**overrides):
    cfg = VisionSolverConfig(
        endpoint="http://172.17.172.35:11434",
        model="ornith-1.5:9b",
        context_size=58192,
        timeout=360.0,
        **overrides,
    )
    return OpenAICompatVisionSolver(cfg)


def test_chat_vision_request_shape(fake_transport):
    answer = solver(api_key="secret-key").chat_vision("find the red tiles", b"fakepng")
    assert answer == '{"tiles": [0], "action": "submit"}'

    post = FakeRequests.posts[-1]
    assert post["url"] == "http://172.17.172.35:11434/v1/chat/completions"
    assert post["timeout"] == 360.0
    assert post["headers"]["Authorization"] == "Bearer secret-key"
    payload = post["json"]
    assert payload["model"] == "ornith-1.5:9b"
    assert payload["temperature"] == 0.0
    assert payload["max_tokens"] == 2048
    assert payload["options"] == {"num_ctx": 58192}
    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "find the red tiles"}
    image_part = content[1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(image_part["image_url"]["url"].split(",", 1)[1]) == b"fakepng"


def test_chat_vision_omits_empty_auth_and_context(fake_transport):
    fake_transport.responses = [completion("hi")]
    cfg = VisionSolverConfig(endpoint="http://x", model="m")
    OpenAICompatVisionSolver(cfg).chat_vision("p", b"img")
    payload = FakeRequests.posts[-1]["json"]
    assert "Authorization" not in FakeRequests.posts[-1]["headers"]
    assert "options" not in payload


def test_chat_vision_retries_once_then_succeeds(fake_transport, monkeypatch):
    monkeypatch.setattr(captcha_vision.time, "sleep", lambda s: None)
    fake_transport.responses = [RequestException("lan hiccup"), completion("ok")]
    assert solver().chat_vision("p", b"img") == "ok"
    assert len(FakeRequests.posts) == 2


def test_chat_vision_raises_vision_error_when_endpoint_down(fake_transport, monkeypatch):
    monkeypatch.setattr(captcha_vision.time, "sleep", lambda s: None)
    fake_transport.responses = [RequestException("down"), RequestException("down")]
    with pytest.raises(VisionSolverError):
        solver().chat_vision("p", b"img")


def test_solve_image_grid_parses_model_answer(fake_transport):
    fake_transport.responses = [completion('```json\n{"tiles": [0, 4, 8], "action": "submit"}\n```')]
    solution = solver().solve_image_grid(b"gridpng", "select red", 9, 3, 3)
    assert solution.tiles == (0, 4, 8)
    # the prompt the model saw describes the grid layout
    sent_prompt = FakeRequests.posts[-1]["json"]["messages"][0]["content"][0]["text"]
    assert "9 tiles (3 rows x 3 columns)" in sent_prompt


# --------------------------------------------------------------------------
# the human round loop: every click must be a real pointer event


class RecorderPointer:
    """Duck-typed _XPointer that records every press instead of touching X."""

    def __init__(self):
        self.moves = []
        self.presses = 0
        self._pos = (0, 0)

    def size(self):
        return (1920, 1080)

    def position(self):
        return self._pos

    def move_to(self, x, y):
        self._pos = (x, y)
        self.moves.append((x, y))

    def mouse_down(self):
        self.presses += 1

    def mouse_up(self):
        pass

    def scroll(self, clicks):
        pass


class FakeWorld:
    """State of a challenge page: rounds of grids, then a cleared page."""

    def __init__(self, rounds=2, challenge_url="https://www.google.com/sorry/index?continue=x"):
        self.rounds = rounds
        self.challenge = True
        self.grid_visible = True
        self.url = challenge_url
        self.verify_clicks = 0
        self.skip_clicks = 0


TILE_BOXES = {
    i: {"x": 40 + (i % 3) * 70, "y": 120 + (i // 3) * 70, "width": 60, "height": 60} for i in range(9)
}
VERIFY_BOX = {"x": 60, "y": 350, "width": 80, "height": 30}
SKIP_BOX = {"x": 160, "y": 350, "width": 60, "height": 30}


class FakeLocator:
    def __init__(self, page, css, index=None):
        self.page = page
        self.css = css
        self.index = index

    @property
    def first(self):
        return self

    async def is_visible(self):
        world = self.page.world
        if self.css in (".rc-imageselect-instructions", ".rc-imageselect-target"):
            return world.grid_visible
        if self.css == ".rc-imageselect-tile":
            return world.grid_visible
        if self.css in ("#recaptcha-verify-button", 'button:has-text("Skip")'):
            return world.grid_visible
        return False

    async def count(self):
        world = self.page.world
        if self.css == ".rc-imageselect-tile":
            return 9 if world.grid_visible else 0
        if self.css in ("#recaptcha-verify-button", 'button:has-text("Skip")'):
            return 1 if world.grid_visible else 0
        return 0

    def nth(self, i):
        return FakeLocator(self.page, self.css, index=i)

    async def bounding_box(self):
        if self.css == ".rc-imageselect-tile":
            return TILE_BOXES[self.index or 0]
        if self.css == "#recaptcha-verify-button":
            return VERIFY_BOX
        if self.css == 'button:has-text("Skip")':
            return SKIP_BOX
        return {"x": 20, "y": 100, "width": 240, "height": 240}

    async def inner_text(self):
        return "Select all squares with\ntraffic lights"

    async def screenshot(self):
        return b"fake-grid-png"

    async def evaluate(self, script):
        # tile paint check: every polled img reports loaded
        return True

    # Playwright-only interaction methods are deliberately ABSENT: if the
    # solver ever calls locator.click() instead of the pointer, these tests
    # fail with AttributeError instead of silently passing.


class FakeIframeLocator(FakeLocator):
    """The challenge iframe element as seen from the parent page."""

    async def is_visible(self):
        return self.page.world.grid_visible

    async def screenshot(self):
        return b"fake-widget-png"


class FakeFrameLocator:
    def __init__(self, page):
        self.page = page

    def locator(self, css):
        return FakeLocator(self.page, css)


class FakePage:
    skip_verify = None  # wired only by the skip-button test

    def __init__(self, world):
        self.world = world
        self.evaluate_calls = 0

    @property
    def url(self):
        return self.world.url

    async def content(self):
        return "<html>unusual traffic from your computer network</html>" if self.world.challenge else "<html>results</html>"

    def frame_locator(self, selector):
        return FakeFrameLocator(self)

    def locator(self, selector):
        return FakeIframeLocator(self, selector)

    async def evaluate(self, script):
        self.evaluate_calls += 1
        return {"sx": 0, "sy": 0, "ow": 1200, "ih": 870, "iw": 1200, "oh": 870}

    async def bring_to_front(self):
        pass

    async def wait_for_selector(self, *args, **kwargs):
        raise TimeoutError("no checkbox frame in this fake")

    def click_verify(self):
        """The fake reCAPTCHA: a VERIFY click advances the challenge."""
        world = self.world
        world.verify_clicks += 1
        if world.verify_clicks >= world.rounds:
            world.challenge = False
            world.grid_visible = False
            world.url = "https://www.google.com/search?q=test"


class VerifyingFrameLocator(FakeFrameLocator):
    """Wires the fake VERIFY button click into the page state machine."""

    def locator(self, css):
        locator = super().locator(css)
        if css == "#recaptcha-verify-button":
            return ClickThroughLocator(locator, self.page.click_verify)
        if css == 'button:has-text("Skip")':
            return ClickThroughLocator(locator, self.page.skip_verify)
        return locator


class ClickThroughLocator(FakeLocator):
    def __init__(self, inner, on_click):
        super().__init__(inner.page, inner.css, inner.index)
        self._inner = inner
        self._on_click = on_click
        self._fired = False

    async def bounding_box(self):
        # last probe before the real click: the state change happens here
        if not self._fired:
            self._fired = True
            self._on_click()
        return await self._inner.bounding_box()

    async def is_visible(self):
        return await self._inner.is_visible()

    async def count(self):
        return await self._inner.count()


def skip_click(world):
    world.skip_clicks += 1
    world.challenge = False
    world.grid_visible = False
    world.url = "https://www.google.com/search?q=test"


class SkipFrameLocator(FakeFrameLocator):
    def locator(self, css):
        locator = super().locator(css)
        if css == 'button:has-text("Skip")':
            return ClickThroughLocator(locator, lambda: skip_click(self.page.world))
        return locator


class StubSolver:
    def __init__(self, solutions):
        self._solutions = list(solutions)
        self.calls = []
        self.cfg = VisionSolverConfig(endpoint="http://stub", model="stub", max_rounds=5)

    def describe(self):
        return "stub @ http://stub"

    def solve_image_grid(self, png, instruction, tile_count, rows, cols):
        self.calls.append({"png": png, "instruction": instruction, "tile_count": tile_count, "rows": rows, "cols": cols})
        solution = self._solutions.pop(0) if len(self._solutions) > 1 else self._solutions[0]
        if isinstance(solution, Exception):
            raise solution
        return solution


@pytest.fixture()
def fast_pacing(monkeypatch):
    """Collapse the human pacing sleeps so round loops run quickly."""
    rand = human_input.random
    monkeypatch.setattr(rand, "uniform", lambda a, b: a)
    monkeypatch.setattr(rand, "randint", lambda a, b: a)
    monkeypatch.setattr(rand, "random", lambda: 0.9)  # never the double-take branch


@pytest.fixture()
def verifying_page():
    world = FakeWorld(rounds=2)
    page = FakePage(world)
    page.frame_locator = lambda selector: VerifyingFrameLocator(page)  # type: ignore[method-assign]
    return page


def test_round_loop_clicks_tiles_and_submits_with_real_mouse(verifying_page, fast_pacing):
    async def run():
        stub = StubSolver([captcha_vision.GridSolution(tiles=(1, 5), action="submit")])
        pointer = RecorderPointer()
        solved = await human_input.human_solve_image_challenge(verifying_page, pointer, solver=stub, max_rounds=5)
        assert solved is True
        # two rounds, each: 2 tile clicks + 1 VERIFY press = 6 real mouse presses
        assert pointer.presses == 6
        assert verifying_page.world.verify_clicks == 2
        # the mouse actually traveled (Bezier moves), not just pressed
        assert len(pointer.moves) > 6
        assert len(stub.calls) == 2
        assert stub.calls[0]["tile_count"] == 9
        assert stub.calls[0]["rows"] == 3 and stub.calls[0]["cols"] == 3
        assert "traffic lights" in stub.calls[0]["instruction"]

    return asyncio.run(run())


def test_round_loop_without_solver_is_a_fast_no(page_without_grid, fast_pacing):
    async def run():
        pointer = RecorderPointer()
        assert await human_input.human_solve_image_challenge(page_without_grid, pointer) is False
        assert pointer.presses == 0

    return asyncio.run(run())


def test_rounds_exhausted_returns_false(fast_pacing):
    async def run():
        world = FakeWorld(rounds=99)  # the challenge never clears
        page = FakePage(world)
        page.frame_locator = lambda selector: VerifyingFrameLocator(page)  # type: ignore[method-assign]
        stub = StubSolver([captcha_vision.GridSolution(tiles=(1,), action="submit")])
        pointer = RecorderPointer()
        solved = await human_input.human_solve_image_challenge(page, pointer, solver=stub, max_rounds=2)
        assert solved is False
        assert pointer.presses == 4  # two rounds, one tile + one verify each
        assert world.verify_clicks == 2

    return asyncio.run(run())


def test_skip_action_presses_skip_button(fast_pacing):
    async def run():
        world = FakeWorld(rounds=1)
        page = FakePage(world)
        page.skip_verify = lambda: skip_click(world)
        page.frame_locator = lambda selector: SkipFrameLocator(page)  # type: ignore[method-assign]
        stub = StubSolver([captcha_vision.GridSolution(tiles=(), action="skip")])
        pointer = RecorderPointer()
        solved = await human_input.human_solve_image_challenge(page, pointer, solver=stub, max_rounds=3)
        assert solved is True
        assert pointer.presses == 1  # only the SKIP press, no tile clicks
        assert world.skip_clicks == 1
        assert world.verify_clicks == 0

    return asyncio.run(run())


def test_vision_transport_failure_aborts_round_loop(verifying_page, fast_pacing):
    async def run():
        stub = StubSolver([VisionSolverError("endpoint down")])
        pointer = RecorderPointer()
        solved = await human_input.human_solve_image_challenge(verifying_page, pointer, solver=stub, max_rounds=5)
        assert solved is False
        assert pointer.presses == 0

    return asyncio.run(run())


@pytest.fixture()
def page_without_grid():
    world = FakeWorld(rounds=1)
    world.challenge = False
    world.grid_visible = False
    world.url = "https://www.bing.com/search?q=test"
    page = FakePage(world)
    page.frame_locator = lambda selector: VerifyingFrameLocator(page)  # type: ignore[method-assign]
    return page


def test_plain_page_has_no_image_grid(page_without_grid, fast_pacing):
    async def run():
        assert await human_input._wait_for_image_grid(page_without_grid, challenge_expected=False) is None

    return asyncio.run(run())


def test_grid_shape_mappings():
    assert human_input._grid_shape(9) == (3, 3)
    assert human_input._grid_shape(16) == (4, 4)
    assert human_input._grid_shape(4) == (2, 2)
    assert human_input._grid_shape(3) == (3, 1)


def test_clear_challenge_false_on_plain_page(page_without_grid, fast_pacing):
    async def run():
        pointer = RecorderPointer()
        assert await human_input.human_clear_challenge(page_without_grid, pointer) is False
        assert pointer.presses == 0

    return asyncio.run(run())


# --------------------------------------------------------------------------
# live endpoint: the deployment parameters, against the real model


def _live_endpoint_up() -> bool:
    try:
        with socket.create_connection(("172.17.172.35", 11434), timeout=1.0):
            return True
    except OSError:
        return False


def _synthetic_grid_png() -> bytes:
    """A 3x3 grid with red tiles on the main diagonal, like the real thing."""
    W = H = 192

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    colors = {0: (255, 0, 0), 1: (0, 128, 255), 2: (255, 255, 255)}
    tiles = [0, 1, 2, 1, 0, 1, 2, 1, 0]
    rows = []
    for y in range(H):
        row = bytearray([0])
        for x in range(W):
            idx = (y // 64) * 3 + (x // 64)
            if x % 64 < 2 or y % 64 < 2:
                row += bytes((40, 40, 40))
            else:
                row += bytes(colors[tiles[idx]])
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


@pytest.mark.skipif(not _live_endpoint_up(), reason="vision endpoint 172.17.172.35:11434 unreachable")
def test_live_endpoint_solves_synthetic_grid():
    cfg = VisionSolverConfig(
        endpoint="http://172.17.172.35:11434",
        model="ornith-1.5:9b",
        context_size=58192,
        timeout=360.0,
    )
    solver_live = OpenAICompatVisionSolver(cfg)
    solution = solver_live.solve_image_grid(
        _synthetic_grid_png(), "Select all squares with red color", 9, 3, 3
    )
    assert solution.tiles, "model returned no tiles"
    assert all(0 <= t < 9 for t in solution.tiles)
    assert solution.action == "submit"
    assert set(solution.tiles) & {0, 4, 8}, f"expected the red diagonal, got {solution.tiles}"
