# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vision-assisted solving of image-grid challenges (reCAPTCHA image select,
hCaptcha task grids, ...).

A standalone capability of the browser-masquerade fetch pool, tied to no
external project: when the human input flow hits a challenge that escalates
to an image grid, the widget screenshot is sent to any vision-capable model
behind a *standard OpenAI-compatible* endpoint (``POST
{endpoint}/v1/chat/completions`` with ``image_url`` content parts). The
model's answer (which tiles match) is carried out by the human input layer:
every click is a real XTEST mouse event, never a programmatic one (see
searx.network.human_input).

Answers are sampled ``votes`` times at rising temperature and decided by
per-tile quorum (strict majority): local vision models localize the right
region reliably but flicker on a tile or two between samples, and a wrong
click escalates the challenge while a missed one is often forgiven.

The grid is presented to the model as one image strip per row, cropped from
the widget screenshot using the tiles' DOM rectangles: models bind "which
photo in this strip" far more reliably than absolute tile numbers over a
composite widget.

Configuration (settings.yml)::

    outgoing:
      captcha_vision:
        enabled: true
        endpoint: 'http://localhost:11434'   # base URL, /v1/chat/completions appended
        api_key: ''                          # optional Bearer token
        model: 'my-vision-model'
        context_size: 58192                  # num_ctx hint (Ollama); 0 = backend default
        timeout: 360                         # seconds per vision call
        max_rounds: 12                       # slide changes/captcha screens per solve attempt
        votes: 6                             # samples per round, quorum-decided; odd ones inverted

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
    "build_row_strips",
    "crop_cells",
    "derive_grid_layout",
    "get_vision_solver",
]

import base64
import json
import re
import struct
import time
import zlib
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
    """``submit`` (press VERIFY) or ``skip`` (press VERIFY when no tile matches)."""

    raw: str = ""
    """The model's raw answer texts, for the log when a round fails."""


@dataclass(frozen=True)
class VisionSolverConfig:
    """Connection parameters for an OpenAI-compatible vision endpoint."""

    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    context_size: int = 0
    timeout: float = 360.0
    max_rounds: int = 12
    votes: int = 1
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
            max_rounds=int(get_setting("outgoing.captcha_vision.max_rounds", 12) or 12),
            votes=int(get_setting("outgoing.captcha_vision.votes", 1) or 1),
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


def widget_prompt(instruction: str, tile_count: int, rows: int, cols: int, negate: bool = False) -> str:
    """Fallback prompt when the widget screenshot could not be cut into row
    strips: the model sees the whole widget and must number the grid itself."""
    pick = (
        "select every tile whose photo contains NO part of that object; if"
        " every tile contains the object, answer with an empty list"
        if negate else
        "select every tile whose photo contains that object; a partly"
        " visible object still counts"
    )
    return (
        "You are helping a person pass an image CAPTCHA challenge. The image shows "
        "the full challenge widget: a blue instruction header (may be in ANY language, "
        f"often not English), a photo grid of {tile_count} tiles ({rows} rows x {cols} "
        "columns) below it, and a button bar at the bottom. Only the photo grid "
        "counts: number its tiles left-to-right, top-to-bottom starting at 0 (the "
        "tile row directly under the header is row 0).\n"
        f"The challenge instruction also reads: {instruction}\n"
        "Work out what object the instruction asks for, then "
        + pick + ". Answer "
        'immediately with strict JSON only, no other text: {"tiles": [..], "action": "submit"}'
    )


def strip_prompt(instruction: str, rows: int, cols: int, negate: bool = False) -> str:
    """Prompt for the per-row strip images: the model only reports positions
    within each strip, which it binds far more reliably than absolute tile
    numbers over a composite widget. ``negate`` inverts the question (which
    photos contain NO part of the object) so the quorum can cross-check."""
    pick = (
        "for each row strip list the positions whose photo contains NO part of "
        "that object (a picture OF a sign, icon or logo contains no part of "
        "it). If every photo in a strip contains the object, give that strip "
        "an empty list; never guess"
        if negate else
        "for each row strip list the positions whose photo contains any part of "
        "that object; a partly visible object still counts. Beware decoys: a "
        "picture OF a sign, icon or logo is not the object itself. If NO photo "
        "in a strip contains the object, give that strip an empty list; never "
        "guess"
    )
    return (
        "You are helping a person pass an image CAPTCHA challenge. You receive one "
        "image strip per grid row, in order: the first image is row 0, the second is "
        f"row 1, and so on ({rows} rows). Each strip shows that row's {cols} tile "
        f"photos left to right, at positions 0 to {cols - 1}.\n"
        f"The challenge instruction (may be in ANY language, often not English) reads: "
        f"{instruction}\n"
        "Work out what object the instruction asks for, then " + pick + ". Do NOT "
        "reason step by step. Answer immediately with strict JSON only, no other "
        'text, one inner list per row in order: {"rows": [[..], [..]]}'
    )


def cell_prompt(instruction: str, tile_count: int, negate: bool = False) -> str:
    """Prompt for per-cell images, the fallback for layouts that do not form
    a uniform grid. ``negate`` inverts the question (see :py:func:`strip_prompt`)."""
    pick = (
        "list every tile photo that contains NO part of that object (a picture"
        " OF a sign, icon or logo contains no part of it). If every tile photo"
        " contains the object, answer with an empty list; never guess"
        if negate else
        "select every tile photo that contains any part of that object; a"
        " partly visible object still counts. Beware decoys: a picture OF a"
        " sign, icon or logo is not the object itself. If NO tile photo"
        " contains the object, answer with an empty list; never guess"
    )
    return (
        "You are helping a person pass an image CAPTCHA challenge. You receive "
        f"{tile_count} tile photos in order: the first image is tile 0, the second "
        f"image is tile 1, and so on (the last image is tile {tile_count - 1}).\n"
        f"The challenge instruction (may be in ANY language, often not English) reads: "
        f"{instruction}\n"
        "Work out what object the instruction asks for, then " + pick + ". Do NOT "
        "reason step by step. Answer immediately with strict JSON only, no other "
        'text: {"tiles": [..], "action": "submit"}'
    )


class OpenAICompatVisionSolver:
    """Asks a vision model over the OpenAI chat completions schema."""

    def __init__(self, cfg: VisionSolverConfig):
        self.cfg = cfg

    def describe(self) -> str:
        return f"{self.cfg.model} @ {self.cfg.endpoint}"

    # transport

    def chat_vision(self, prompt: str, images: list[bytes], mime: str = "image/png", temperature: float | None = None) -> str:
        """One chat completion with attached images; returns the assistant text."""
        content: list[dict] = [{"type": "text", "text": prompt}]
        for image in images:
            data_uri = "data:%s;base64,%s" % (mime, base64.b64encode(image).decode("ascii"))
            content.append({"type": "image_url", "image_url": {"url": data_uri}})
        payload: dict = {
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": self.cfg.temperature if temperature is None else temperature,
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
        text = message.get("content") or ""
        if not text.strip():
            # reasoning models (Ollama moves the thinking into a separate
            # field) can exhaust max_tokens before writing the answer; their
            # conclusion still holds the JSON the parser looks for
            text = message.get("reasoning") or ""
        return text

    # challenge-level API

    def solve_grid(
        self,
        widget_png: bytes,
        grid_images: "tuple[str, list[bytes]] | None",
        instruction: str,
        tile_count: int,
        rows: int,
        cols: int,
    ) -> GridSolution:
        """Decide which tiles of the challenge grid match ``instruction``.

        ``grid_images`` selects how the grid is presented to the model:
        ``("rows", strips)`` (one strip per row, the best-bound presentation
        for uniform grids), ``("cells", tiles)`` (one image per tile, the
        fallback for irregular layouts) or ``None`` (the whole widget
        screenshot). The presentation is decided by the caller from the
        tiles' DOM geometry.

        The answer is sampled ``cfg.votes`` times (first vote at the
        configured temperature, the rest at rising temperatures to
        decorrelate the answers). Odd votes ask the INVERTED question
        (which tiles contain no part of the object); their answers are
        mapped back through the complement before the quorum, so a
        hallucinated tile must be wrong twice, in opposite directions, to
        reach a strict majority. The tiles kept are those that reach that
        quorum.
        """
        if grid_images is None:
            mode, images = "widget", [widget_png]
        else:
            mode, images = grid_images

        def build_prompt(negate: bool) -> str:
            if mode == "rows":
                return strip_prompt(instruction, rows, cols, negate=negate)
            if mode == "cells":
                return cell_prompt(instruction, tile_count, negate=negate)
            return widget_prompt(instruction, tile_count, rows, cols, negate=negate)

        def parse(text: str) -> GridSolution:
            if mode == "rows":
                return self.parse_strip_solution(text, rows, cols)
            return self.parse_solution(text, tile_count)

        # the valid tile positions: strips report row-major indices over the
        # whole grid, cells and the widget report plain tile indices
        universe = rows * cols if mode == "rows" else tile_count

        votes = max(1, self.cfg.votes)
        per_vote: list[set[int]] = []
        for vote in range(votes):
            # vote 0 pins the configured (usually greedy) temperature; the
            # rest sample around it so the quorum can separate a stable
            # answer from a flickering one
            negate = vote % 2 == 1
            temperature = self.cfg.temperature if vote == 0 else min(0.8, 0.25 * vote)
            try:
                answer = self.chat_vision(build_prompt(negate), images, temperature=temperature)
                solution = parse(answer)
            except VisionSolverError as err:
                logger.warning("vision vote %s/%s unusable: %s", vote + 1, votes, err)
                continue
            seen = set(solution.tiles)
            if negate:
                # invert back to "tiles WITH the object"
                mapped = set(range(universe)) - seen
            else:
                mapped = seen
            logger.info(
                "vision vote %s/%s (temp %.2f%s): tiles=%s",
                vote + 1,
                votes,
                temperature,
                ", inverted" if negate else "",
                sorted(mapped),
            )
            per_vote.append(mapped)

        if not per_vote:
            raise VisionSolverError("no usable answer in %s vision vote(s)" % votes)
        threshold = len(per_vote) // 2 + 1
        counts: dict[int, int] = {}
        for tiles in per_vote:
            for tile in tiles:
                counts[tile] = counts.get(tile, 0) + 1
        quorum = tuple(sorted(t for t, c in counts.items() if c >= threshold))
        if len(per_vote) > 1 and not quorum:
            logger.warning(
                "vision quorum empty over %s votes (per-vote: %s) -- treating as skip",
                len(per_vote),
                [sorted(v) for v in per_vote],
            )
        action = "submit" if quorum else "skip"
        return GridSolution(tiles=quorum, action=action, raw=" | ".join(str(sorted(v)) for v in per_vote))

    # parsing

    @staticmethod
    def parse_solution(text: str, tile_count: int) -> GridSolution:
        """Parse ``{"tiles": [..], "action": ".."}`` out of a model answer.

        Tolerates markdown fences and prose around the JSON; falls back to
        collecting bare integers from short answers. Tile indices outside the
        grid are dropped, duplicates collapsed, order kept.
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
        if not tiles and len(raw) <= 200:
            # last resort for short free-form answers ("tiles 1 and 7 look
            # right"): bare integers. Gated by length so reasoning prose --
            # which is full of digits that are not answers -- never feeds
            # the click list.
            tiles = [int(t) for t in re.findall(r"\b\d+\b", raw)]
        valid = []
        for t in tiles:
            if 0 <= t < tile_count and t not in valid:
                valid.append(t)
        if not candidates and not valid and action == "submit":
            raise VisionSolverError("no usable answer in vision output: %r" % raw[:200])
        if not valid and action == "submit":
            # a submit with nothing selected would only burn a round: treat
            # the empty selection as the skip it de facto is
            action = "skip"
        return GridSolution(tiles=tuple(valid), action=action, raw=raw)

    @staticmethod
    def parse_strip_solution(text: str, rows: int, cols: int) -> GridSolution:
        """Parse ``{"rows": [[..], ..]}`` (positions within each row strip)
        into absolute row-major tile indices. Accepts a flat ``tiles`` list
        too, for models that ignore the strip layout."""
        raw = (text or "").strip()
        candidates = re.findall(r"\{.*\}", raw, re.DOTALL)
        tiles: list[int] = []
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            grid = data.get("rows")
            if isinstance(grid, list):
                for row_index, positions in enumerate(grid[:rows]):
                    if not isinstance(positions, list):
                        continue
                    for position in positions:
                        if isinstance(position, int) and 0 <= position < cols:
                            tiles.append(row_index * cols + position)
                break
            raw_tiles = data.get("tiles")
            if isinstance(raw_tiles, list):
                tiles = [t for t in raw_tiles if isinstance(t, int)]
                break
        valid = []
        for t in tiles:
            if 0 <= t < rows * cols and t not in valid:
                valid.append(t)
        if not candidates:
            raise VisionSolverError("no usable answer in vision output: %r" % raw[:200])
        action = "submit" if valid else "skip"
        return GridSolution(tiles=tuple(valid), action=action, raw=raw)


# --------------------------------------------------------------------------
# PNG cropping (pure stdlib: the image has no Pillow, and the solver only
# needs to cut the widget screenshot into row strips)


def _png_decode(png: bytes) -> tuple[int, int, int, bytes]:
    """Decode an 8-bit, non-interlaced RGB/RGBA PNG into raw pixels."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise VisionSolverError("not a PNG")
    pos, idat, header = 8, b"", None
    while pos + 12 <= len(png):
        length, tag = struct.unpack(">I4s", png[pos:pos + 8])
        chunk = png[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", chunk[:10])
            header = (width, height, depth, color)
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + length
    if header is None:
        raise VisionSolverError("PNG has no header")
    width, height, depth, color = header
    if depth != 8 or color not in (2, 6):
        raise VisionSolverError("unsupported PNG: depth %s color %s" % (depth, color))
    bpp = 4 if color == 6 else 3
    stride = width * bpp
    raw = zlib.decompress(idat)
    pixels, previous = bytearray(), bytes(stride)
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        line = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        if filter_type == 1:  # sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:  # up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:  # average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = previous[i]
                c = previous[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + predictor) & 0xFF
        elif filter_type != 0:
            raise VisionSolverError("unsupported PNG filter %s" % filter_type)
        pixels += line
        previous = bytes(line)
    return width, height, bpp, bytes(pixels)


def _png_encode(width: int, height: int, bpp: int, pixels: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    stride = width * bpp
    rows = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6 if bpp == 4 else 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def crop_png(png: bytes, x: int, y: int, width: int, height: int) -> bytes:
    """Crop a rectangle out of a PNG, clamped to the image."""
    img_w, img_h, bpp, pixels = _png_decode(png)
    x, y = max(0, x), max(0, y)
    width = min(width, img_w - x)
    height = min(height, img_h - y)
    if width <= 0 or height <= 0:
        raise VisionSolverError("empty crop")
    stride = img_w * bpp
    out = bytearray()
    for row in range(height):
        start = (y + row) * stride + x * bpp
        out += pixels[start:start + width * bpp]
    return _png_encode(width, height, bpp, bytes(out))


def stitch_horizontal(pngs: list[bytes]) -> bytes:
    """Concatenate same-size PNGs left to right into one strip."""
    decoded = [_png_decode(png) for png in pngs]
    height = decoded[0][1]
    bpp = decoded[0][2]
    if any(h != height or b != bpp for _, h, b, _ in decoded):
        raise VisionSolverError("cannot stitch images of different sizes")
    width = sum(w for w, _, _, _ in decoded)
    stride_out = width * bpp
    out = bytearray(stride_out * height)
    offset = 0
    for img_w, _, _, pixels in decoded:
        stride_in = img_w * bpp
        for row in range(height):
            start = row * stride_out + offset
            out[start:start + stride_in] = pixels[row * stride_in:(row + 1) * stride_in]
        offset += stride_in
    return _png_encode(width, height, bpp, bytes(out))


def build_row_strips(widget_png: bytes, rects: list[list[int]], cols: int, inset: int = 3) -> list[bytes]:
    """Cut the widget screenshot into one strip per grid row.

    ``rects`` are the tiles' bounding rectangles in the same coordinate
    space as the screenshot (the iframe viewport), in DOM order (row-major)
    for a uniform ``cols``-wide grid. A small inset trims the tile borders
    so the model sees clean photos.
    """
    if not rects or len(rects) % cols != 0:
        raise VisionSolverError("tile rectangles do not match the grid")
    tiles = [
        crop_png(widget_png, x + inset, y + inset, max(4, w - 2 * inset), max(4, h - 2 * inset))
        for x, y, w, h in rects
    ]
    rows = len(rects) // cols
    return [stitch_horizontal(tiles[r * cols:(r + 1) * cols]) for r in range(rows)]


def crop_cells(widget_png: bytes, rects: list[list[int]], inset: int = 3) -> list[bytes]:
    """Cut the widget screenshot into one image per tile, DOM order. The
    fallback presentation for layouts that are not uniform grids."""
    return [
        crop_png(widget_png, x + inset, y + inset, max(4, w - 2 * inset), max(4, h - 2 * inset))
        for x, y, w, h in rects
    ]


def derive_grid_layout(rects: list[list[int]]) -> "tuple[int, int] | None":
    """(rows, cols) derived from the tiles' own geometry, so any reasonable
    grid dimension works without a hardcoded size map. None when the tiles
    do not form a uniform grid (the caller then falls back to per-cell
    presentation)."""
    if not rects:
        return None
    heights = sorted(h for _, _, _, h in rects)
    widths = sorted(w for _, _, w, _ in rects)
    tile_h, tile_w = heights[len(heights) // 2], widths[len(widths) // 2]

    def cluster_bands(indices: list[int], centers: list[float], tolerance: float) -> list[list[int]]:
        bands: list[list[int]] = []
        previous = 0.0
        for index in sorted(indices, key=lambda i: centers[i]):
            center = centers[index]
            if not bands or center - previous > tolerance:
                bands.append([])
            bands[-1].append(index)
            previous = center
        return bands

    order = list(range(len(rects)))
    rows_bands = cluster_bands(order, [y + h / 2 for _, y, _, h in rects], tile_h * 0.4)
    cols_bands = cluster_bands(order, [x + w / 2 for x, _, w, _ in rects], tile_w * 0.4)
    rows, cols = len(rows_bands), len(cols_bands)
    if rows * cols != len(rects):
        return None
    cells = set()
    row_of = {i: r for r, band in enumerate(rows_bands) for i in band}
    col_of = {i: c for c, band in enumerate(cols_bands) for i in band}
    for i in order:
        cells.add((row_of[i], col_of[i]))
    if len(cells) != len(rects):
        return None
    return rows, cols
