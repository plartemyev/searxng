# SPDX-License-Identifier: AGPL-3.0-or-later
"""Global per-provider pacing for outgoing engine requests.

Inside one SearXNG process, concurrent searches easily reach the same
provider at the same moment: two agents querying at once both fan out to
google, and a single search may hit a provider through more than one engine
(web + images). Providers read near-simultaneous requests as bot traffic,
so before an engine request is sent its provider must be free of a pending
slot, and sends are spaced a random ``delay_min``..``delay_max`` seconds
apart.

Pacing is keyed by upstream host (``www.`` stripped), so requests to
different providers never block each other. Reserving a slot is O(1) under
a short lock; the caller sleeps outside it, so a slow upstream fetch never
head-of-line blocks queued requests. A queue is bounded by ``max_wait``:
past that, slots are pulled into the present so bursts drain instead of
queueing forever.
"""

from __future__ import annotations

__all__ = ["pace_request", "reserve_send_slot", "pace_key_for_url"]

import asyncio
import logging
import random
import threading
import time
from urllib.parse import urlsplit

from searx import get_setting

logger = logging.getLogger('searx.network.pacing')

_slot_lock = threading.Lock()
_next_slot_by_key: dict[str, float] = {}


def pace_key_for_url(url: str) -> str:
    """Return the provider key for a request URL: its host, sans ``www.``."""
    host = urlsplit(url).hostname or ''
    host = host.lower()
    if host.startswith('www.'):
        host = host[len('www.'):]
    return host


def reserve_send_slot(
    key: str, *, delay_min: float, delay_max: float, max_wait: float
) -> float:
    """Reserve the next send slot for ``key``; return how long to sleep first.

    A cold provider (no reserved slot, or the last one already spent) sends
    immediately: pacing sits between requests, not in front of the first
    one. Concurrent requests chain up: each waits for the previous slot plus
    a random ``delay_min``..``delay_max`` gap, so sends to the same provider
    are spaced that far apart whatever engine or search sent them.

    A request that already queued for ``max_wait`` gets a slot capped at
    ``now + max_wait`` so a pile-up cannot stall requests forever; slots
    never move backwards.
    """
    with _slot_lock:
        now = time.monotonic()
        gap = random.uniform(delay_min, delay_max)  # noqa: S311
        next_free = _next_slot_by_key.get(key, 0.0)
        if now >= next_free:
            # provider idle: send now, re-arm the chain for the next one
            _next_slot_by_key[key] = now + gap
            return 0.0
        slot = min(next_free + gap, now + max_wait)
        wait = max(0.0, slot - now)
        _next_slot_by_key[key] = max(slot, now)
        return wait


async def pace_request(url: str) -> float:
    """Sleep until ``url``'s provider send slot is free.

    Called for every non-stream engine request (see
    :py:meth:`searx.network.Network.call_client`). Returns the wait in
    seconds (0 when the slot was free).
    """
    if not get_setting('outgoing.pacing.enabled', True):
        return 0.0
    key = pace_key_for_url(url)
    if not key:
        return 0.0
    wait = reserve_send_slot(
        key,
        delay_min=get_setting('outgoing.pacing.delay_min_seconds', 1.0),
        delay_max=get_setting('outgoing.pacing.delay_max_seconds', 5.0),
        max_wait=get_setting('outgoing.pacing.max_wait_seconds', 20.0),
    )
    if wait > 0:
        logger.debug('pacing %s: waiting %.2fs for a send slot', key, wait)
        await asyncio.sleep(wait)
    return wait
