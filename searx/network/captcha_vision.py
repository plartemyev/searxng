# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vision-assisted solving of image-grid challenges (reCAPTCHA image select,
hCaptcha task grids, ...).

A standalone capability of the browser-masquerade fetch pool, tied to no
external project: when the human input flow hits a challenge that escalates
to an image grid, the grid screenshot and its instruction are sent to any
vision-capable model behind a *standard OpenAI-compatible* endpoint
(``POST {endpoint}/v1/chat/completions`` with ``image_url`` content parts).
The model's answer (which tiles match) is carried out by the human input
layer: every click is a real XTEST mouse event, never a programmatic one
(see searx.network.human_input).

Configuration (settings.yml)::

    outgoing:
      captcha_vision:
        enabled: true
        endpoint: 'http://localhost:11434'   # base URL, /v1/chat/completions appended
        api_key: ''                          # optional Bearer token
        model: 'my-vision-model'
        context_size: 58192                  # num_ctx hint (Ollama); 0 = backend default
        timeout: 360                         # seconds per vision call
        max_rounds: 5                        # challenge rounds per solve attempt

Any server that implements the OpenAI chat completions schema works; extra
fields the server does not know (like the ``options`` context hint) are
tolerated by common implementations (Ollama, vLLM, llama.cpp) and ignored
otherwise.
"""

from __future__ import annotations

__all__ = [
    "GridSolution",
    "OpenAICompatVisionSolver",
    "VisionSolverConfig",
    "VisionSolverError",
    "get_vision_solver",
]

import base64
import json
import re
import time
from dataclasses import dataclass

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from searx import get_setting, logger

logger = logger.getChild('network.captcha_vision')


class VisionSolverError(Exception):
    """The vision model could not be asked or its answer was unusable."""


@dataclass(frozen=True)
class GridSolution:
    """What the vision model says to do with one challenge round."""

    tiles: tuple[int, ...]
    """Tile indices to click, row-major from 0 (validated against the grid)."""

    action: str
    """``submit`` (press VERIFY) or ``skip`` (press SKIP / VERIFY when no tile matches)."""

    raw: str = ""
    """The model's raw answer text, for the log when a round fails."""


@dataclass(frozen=True)
class VisionSolverConfig:
    """Connection parameters for an OpenAI-compatible vision endpoint."""

    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    context_size: int = 0
    timeout: float = 360.0
    max_rounds: int = 5
    temperature: float = 0.0
    max_tokens: int = 2048

    @classmethod
    def from_settings(cls) -> "VisionSolverConfig":
        return cls(
            endpoint=(get_setting("outgoing.captcha_vision.endpoint", "") or "").strip(),
            model=(get_setting("outgoing.captcha_vision.model", "") or "").strip(),
            api_key=(get_setting("outgoing.captcha_vision.api_key", "") or "").strip(),
            context_size=int(get_setting("outgoing.captcha_vision.context_size", 0) or 0),
            timeout=float(get_setting("outgoing.captcha_vision.timeout", 360.0) or 360.0),
            max_rounds=int(get_setting("outgoing.captcha_vision.max_rounds", 5) or 5),
            temperature=float(get_setting("outgoing.captcha_vision.temperature", 0.0) or 0.0),
            max_tokens=int(get_setting("outgoing.captcha_vision.max_tokens", 2048) or 2048),
        )

    def is_configured(self) -> bool:
        return bool(self.endpoint and self.model)


def get_vision_solver() -> "OpenAICompatVisionSolver | None":
    """The configured solver, or None when no vision endpoint is set up."""
    cfg = VisionSolverConfig.from_settings()
    if not cfg.is_configured():
        return None
    return OpenAICompatVisionSolver(cfg)


def grid_prompt(instruction: str, tile_count: int, rows: int, cols: int) -> str:
    """The prompt sent with the widget screenshot. Kept strict and small:
    local vision models follow short contracts far better than long ones."""
    return (
        "You are helping a person pass an image CAPTCHA challenge. The image shows "
        "the full challenge widget: an instruction line (may be in ANY language, often "
        f"not English) and a grid of {tile_count} tiles ({rows} rows x {cols} columns). "
        "Number the tiles left-to-right, top-to-bottom starting at 0.\n"
        f"The challenge instruction also reads: {instruction}\n"
        "Work out what object the instruction asks for, then select every tile whose "
        "photo contains that object. Look at each tile individually; a partly visible "
        "object still counts. Answer with strict JSON only, no other text: "
        '{"tiles": [..], "action": "submit"}\n'
        'Use "action": "skip" only when the instruction says to click skip when none '
        "are left AND no tile contains the object."
    )


class OpenAICompatVisionSolver:
    """Asks a vision model over the OpenAI chat completions schema."""

    def __init__(self, cfg: VisionSolverConfig):
        self.cfg = cfg

    def describe(self) -> str:
        return f"{self.cfg.model} @ {self.cfg.endpoint}"

    # transport

    def chat_vision(self, prompt: str, image: bytes, mime: str = "image/png") -> str:
        """One chat completion with one attached image; returns the assistant text."""
        data_uri = "data:%s;base64,%s" % (mime, base64.b64encode(image).decode("ascii"))
        payload: dict = {
            "model": self.cfg.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if self.cfg.context_size > 0:
            # Ollama-style context hint; unknown fields are ignored by
            # servers that do not implement them.
            payload["options"] = {"num_ctx": self.cfg.context_size}
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = "Bearer %s" % self.cfg.api_key

        url = self.cfg.endpoint.rstrip("/") + "/v1/chat/completions"
        data: dict | None = None
        for attempt in (1, 2):  # one retry: cold model loads and LAN hiccups
            try:
                resp = curl_requests.post(url, json=payload, headers=headers, timeout=self.cfg.timeout)
                resp.raise_for_status()
                data = resp.json()
                break
            except (RequestException, ValueError) as err:
                logger.warning("vision request attempt %s failed: %s", attempt, err)
                if attempt == 2:
                    raise VisionSolverError("vision endpoint unreachable: %s" % err) from err
                time.sleep(1.0)
        if data is None:  # pragma: no cover - the loop always sets or raises
            raise VisionSolverError("vision endpoint returned no data")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as err:
            raise VisionSolverError("unexpected vision response shape: %s" % err) from err
        content = message.get("content") or ""
        if not content.strip():
            # reasoning models (Ollama moves the thinking into a separate
            # field) can exhaust max_tokens before writing the answer; their
            # conclusion still holds the JSON the parser looks for
            content = message.get("reasoning") or ""
        return content

    # challenge-level API

    def solve_image_grid(
        self, grid_png: bytes, instruction: str, tile_count: int, rows: int, cols: int
    ) -> GridSolution:
        """Ask the model which tiles of a grid screenshot match ``instruction``."""
        answer = self.chat_vision(grid_prompt(instruction, tile_count, rows, cols), grid_png)
        return self.parse_solution(answer, tile_count)

    # parsing

    @staticmethod
    def parse_solution(text: str, tile_count: int) -> GridSolution:
        """Parse ``{"tiles": [..], "action": ".."}`` out of a model answer.

        Tolerates markdown fences and prose around the JSON; falls back to
        collecting bare integers. Tile indices outside the grid are dropped,
        duplicates collapsed, order kept.
        """
        raw = (text or "").strip()
        candidates = re.findall(r"\{.*\}", raw, re.DOTALL)
        tiles: list[int] = []
        action = "submit"
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            raw_tiles = data.get("tiles")
            if isinstance(raw_tiles, list):
                tiles = [t for t in raw_tiles if isinstance(t, int)]
            elif isinstance(raw_tiles, str):
                # some models answer with "tiles": "0, 4, 8"
                tiles = [int(t) for t in re.findall(r"\d+", raw_tiles)]
            parsed_action = str(data.get("action", "submit")).strip().lower()
            if parsed_action.startswith("skip"):
                action = "skip"
            if raw_tiles is not None:
                break
        if not tiles:
            # last resort for short free-form answers ("tiles 1 and 7 look
            # right"): bare integers. Gated by length so reasoning prose --
            # which is full of digits that are not answers -- never feeds
            # the click list.
            if len(raw) <= 200:
                tiles = [int(t) for t in re.findall(r"\b\d+\b", raw)]
        valid = []
        for t in tiles:
            if 0 <= t < tile_count and t not in valid:
                valid.append(t)
        if not candidates and not valid and action == "submit":
            raise VisionSolverError("no usable answer in vision output: %r" % raw[:200])
        if not valid and action == "submit":
            # a model saying "submit" with nothing selected would only burn a
            # round: treat an empty selection as the skip it de facto is
            action = "skip"
        return GridSolution(tiles=tuple(valid), action=action, raw=raw)
