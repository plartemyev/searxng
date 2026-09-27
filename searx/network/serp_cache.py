# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistent cache for whole SERPs (search result pages).

A ``/search`` request whose canonical key matches a fresh cache entry is
served entirely from disk: the per-engine result blocks are replayed into
the :py:obj:`searx.results.ResultContainer` and no engine request, hence
no browser lane, is touched.

- Key: blake2b-128 over the canonical search identity (query, lang,
  pageno, safesearch, time_range, engine set). Two requests may only
  share an entry when they would have asked the engines for the same
  thing.
- Body: one zstd-compressed JSON envelope holding every engine's parsed
  result block.
- TTL: set on write only, never extended by reads; expired entries are
  misses.

Everything is best-effort: any cache failure logs and falls back to the
uncached path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import typing as t

import msgspec

import zstandard as zstd

from searx import get_setting, logger

if t.TYPE_CHECKING:
    from searx.results import ResultContainer
    from searx.result_types import EngineResults, LegacyResult
    from searx.search.models import SearchQuery


log = logger.getChild("serp_cache")

SCHEMA_VERSION = 2

# fingerprint of one result item: a tagged struct ("Image", "Answer", ..)
# or a legacy free-form dict
_LEGACY_TAG = "legacy"

_JSON_SEPARATORS = (",", ":")


def _data_dir() -> str:
    # same base as the geo-identity cache (see searx.network.browser)
    return os.environ.get("__SEARXNG_DATA_PATH") or "/var/cache/searxng"


def enabled() -> bool:
    return bool(get_setting("outgoing.search_cache.enabled", False))


def _ttl_seconds() -> float:
    return max(1.0, float(get_setting("outgoing.search_cache.ttl_seconds", 24 * 3600)))


def _prune_interval() -> int:
    return max(1, int(get_setting("outgoing.search_cache.prune_interval_writes", 50)))


def _compression_level() -> int:
    return max(1, min(22, int(get_setting("outgoing.search_cache.compression_level", 3))))


def canonical_key(search_query: "SearchQuery") -> str:
    """The cache key for a search request.

    The identity must cover everything that changes what the engines
    would have been asked for: the query (whitespace-normalized), the
    requested locale, paging, safe-search, the time range and the exact
    engine/category set.
    """
    identity = {
        "q": " ".join((search_query.query or "").split()),
        "lang": search_query.lang or "all",
        "pageno": search_query.pageno,
        "safesearch": search_query.safesearch,
        "time_range": search_query.time_range or "",
        "engines": sorted(
            f"{engineref.name}:{engineref.category}"
            for engineref in search_query.engineref_list
        ),
    }
    blob = json.dumps(identity, sort_keys=True, separators=_JSON_SEPARATORS)
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=16).hexdigest()


def dump_results(results: "EngineResults") -> list[dict]:
    """JSON-able snapshots of one engine's parsed results."""
    dumped: list[dict] = []
    for item in list(results):
        try:
            snapshot = msgspec.to_builtins(item)
            if not isinstance(snapshot, dict):
                continue
        except Exception:  # pylint: disable=broad-except
            log.debug("serp-cache: undumpable result item skipped", exc_info=True)
            continue
        # parsed_url is a urllib SplitResult (JSON-unfriendly) and fully
        # derived from url: drop it, normalization rebuilds it on load
        snapshot.pop("parsed_url", None)
        snapshot["__type"] = type(item).__name__
        dumped.append(snapshot)
    return dumped


# container-managed fields: rebuilt by ResultContainer.extend /
# normalize_result_fields, must not be carried through the cache
_CONTAINER_MANAGED_KEYS = ("parsed_url", "engines")


def _legacy_snapshot(snapshot: dict) -> "LegacyResult":
    # pylint: disable=import-outside-toplevel
    from searx.result_types import LegacyResult

    return LegacyResult(
        {k: v for k, v in snapshot.items() if k not in _CONTAINER_MANAGED_KEYS}
    )


def load_results(blocks: list[dict]) -> "list[Result | LegacyResult]":
    """Rebuild result items from their cached snapshots.

    A snapshot whose type cannot be reconstructed degrades to a legacy
    dict result: the container normalizes those the same way it always
    has, so a decode miss costs fidelity, not results.
    """
    # pylint: disable=import-outside-toplevel
    from searx.result_types import LegacyResult, ResultList

    results: list = []
    for snapshot in blocks:
        snapshot = dict(snapshot)
        type_name = snapshot.pop("__type", _LEGACY_TAG)
        if type_name == _LEGACY_TAG:
            results.append(_legacy_snapshot(snapshot))
            continue
        result_cls = getattr(ResultList.types, type_name, None)
        if result_cls is None:
            results.append(_legacy_snapshot(snapshot))
            continue
        try:
            results.append(msgspec.convert(snapshot, result_cls, strict=False))
        except Exception:  # pylint: disable=broad-except
            log.debug("serp-cache: could not rebuild a %s", type_name)
            results.append(_legacy_snapshot(snapshot))
    return results


class _CacheEntry:
    """One cached SERP: per-engine result blocks plus envelope metadata."""

    def __init__(self, key: str, search_query: "SearchQuery", blocks: dict[str, list[dict]]):
        self.key = key
        self.query = search_query.query
        self.lang = search_query.lang
        self.pageno = search_query.pageno
        self.blocks = blocks
        now = time.time()
        self.created_at = now
        self.expires_at = now + _ttl_seconds()


class SerpCache:
    """SQLite-backed store of zstd-compressed SERP envelopes (WAL)."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS serp_cache (
                key TEXT PRIMARY KEY,
                query TEXT,
                lang TEXT,
                pageno INTEGER,
                created_at REAL,
                expires_at REAL,
                payload BLOB
            )
            """
        )
        self._conn.commit()
        self._writes_since_prune = 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pylint: disable=broad-except
            pass

    # -- reads ---------------------------------------------------------------

    def lookup(self, key: str) -> "_CacheEntry | None":
        """The fresh entry for ``key``, or None (miss or expired)."""
        try:
            row = self._conn.execute(
                "SELECT payload FROM serp_cache WHERE key = ? AND expires_at > ?",
                (key, time.time()),
            ).fetchone()
        except sqlite3.Error:
            log.warning("serp-cache: lookup failed", exc_info=True)
            return None
        if row is None:
            return None
        try:
            envelope = json.loads(zstd.ZstdDecompressor().decompress(row[0]))
        except Exception:  # pylint: disable=broad-except
            log.warning("serp-cache: unreadable payload for %s", key)
            return None
        if envelope.get("schema") != SCHEMA_VERSION:
            return None
        entry = _CacheEntry.__new__(_CacheEntry)
        entry.key = key
        entry.query = envelope.get("query", "")
        entry.lang = envelope.get("lang")
        entry.pageno = envelope.get("pageno", 1)
        entry.blocks = envelope.get("blocks", {})
        entry.created_at = envelope.get("created_at", 0.0)
        entry.expires_at = envelope.get("expires_at", 0.0)
        return entry

    # -- writes ---------------------------------------------------------------

    def store(self, entry: _CacheEntry) -> None:
        """Write the full entry; TTL is fixed here and never refreshed."""
        envelope = {
            "schema": SCHEMA_VERSION,
            "query": entry.query,
            "lang": entry.lang,
            "pageno": entry.pageno,
            "created_at": entry.created_at,
            "expires_at": entry.expires_at,
            "blocks": entry.blocks,
        }
        try:
            payload = zstd.ZstdCompressor(level=_compression_level()).compress(
                json.dumps(envelope, separators=_JSON_SEPARATORS).encode("utf-8")
            )
        except Exception:  # pylint: disable=broad-except
            # a serializable-envelope guarantee is the caller's job; a
            # cache must never take the search down with it
            log.warning("serp-cache: envelope serialization failed", exc_info=True)
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO serp_cache"
                " (key, query, lang, pageno, created_at, expires_at, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.key,
                    entry.query,
                    entry.lang,
                    entry.pageno,
                    entry.created_at,
                    entry.expires_at,
                    payload,
                ),
            )
            self._conn.commit()
        except sqlite3.Error:
            log.warning("serp-cache: store failed", exc_info=True)
            return
        self._writes_since_prune += 1
        if self._writes_since_prune >= _prune_interval():
            self._writes_since_prune = 0
            self.prune()

    def prune(self) -> None:
        """Drop expired entries (lazy maintenance, runs on the write path)."""
        try:
            self._conn.execute("DELETE FROM serp_cache WHERE expires_at <= ?", (time.time(),))
            self._conn.commit()
        except sqlite3.Error:
            log.debug("serp-cache: prune failed", exc_info=True)


_cache: SerpCache | None = None


def get_cache() -> SerpCache | None:
    """The process-wide cache, or None when disabled (settings-driven)."""
    # pylint: disable=global-statement
    global _cache
    if not enabled():
        return None
    if _cache is None:
        path = str(
            get_setting("outgoing.search_cache.path")
            or os.path.join(_data_dir(), "serp_cache.sqlite3")
        )
        try:
            _cache = SerpCache(path)
            log.info("serp-cache: open at %s (ttl %.0fs)", path, _ttl_seconds())
        except Exception:  # pylint: disable=broad-except
            log.warning("serp-cache: cannot open %s; caching disabled", path, exc_info=True)
            return None
    return _cache


class RequestContext:
    """Per-request cache state: read on construction, write on finish.

    Lives on the :py:obj:`searx.search.Search` flow. ``apply`` replays the
    cached engine blocks and returns the requests that still need to run;
    ``finish`` captures what the fan-out produced and stores the entry.
    """

    def __init__(self, search_query: "SearchQuery"):
        self.cache = get_cache()
        self.key = canonical_key(search_query)
        self.query = search_query.query
        self.lang = search_query.lang
        self.pageno = search_query.pageno
        self.entry = self.cache.lookup(self.key) if self.cache else None
        self.stored = False

    def apply(
        self, result_container: "ResultContainer", requests: list[tuple[str, str, dict]]
    ) -> list[tuple[str, str, dict]]:
        """Replay cached blocks; return the requests still to be sent.

        Replays are fault-isolated per engine: a block that cannot be
        rebuilt degrades to a live fetch of that engine, never to a failed
        search.
        """
        replayed: list[str] = []
        remaining: list[tuple[str, str, dict]] = []
        if self.entry is not None:
            for request in requests:
                engine_name = request[0]
                block = self.entry.blocks.get(engine_name)
                if block is None:
                    remaining.append(request)
                    continue
                try:
                    result_container.extend(engine_name, load_results(block))
                    replayed.append(engine_name)
                except Exception:  # pylint: disable=broad-except
                    log.warning(
                        "serp-cache: replay of %s failed; fetching live",
                        engine_name,
                        exc_info=True,
                    )
                    remaining.append(request)
            if replayed:
                log.info(
                    "serp-cache: HIT key=%s replayed=%s fetching=%d",
                    self.key, ",".join(replayed), len(remaining),
                )
        else:
            remaining = requests
        if remaining:
            # capture what the fan-out produces so finish can store or
            # merge the missing engine blocks (partial hits self-heal)
            result_container.serp_cache_blocks = {}
        else:
            self.stored = True  # nothing new to write
        return remaining

    def finish(self, result_container: "ResultContainer") -> None:
        """Store/merge the entry from the engine blocks the run produced."""
        if self.cache is None or self.stored:
            return
        blocks = getattr(result_container, "serp_cache_blocks", None)
        if blocks is None:
            return
        fresh: dict[str, list[dict]] = {}
        for name, raw_results in blocks.items():
            if not name or not raw_results:
                continue
            try:
                fresh[name] = dump_results(raw_results)
            except Exception:  # pylint: disable=broad-except
                log.debug("serp-cache: serializing %s failed", name, exc_info=True)
        if not fresh:
            return
        if self.entry is not None:
            # partial hit: add the engines that were missing; the entry's
            # expiry stays where its first write put it (set on write only)
            merged = dict(self.entry.blocks)
            merged.update(fresh)
        else:
            merged = fresh
        entry = _CacheEntry(self.key, _QueryView(self.query, self.lang, self.pageno), merged)
        if self.entry is not None:
            entry.created_at = self.entry.created_at
            entry.expires_at = self.entry.expires_at
        self.cache.store(entry)
        log.info("serp-cache: STORE key=%s engines=%s", self.key, ",".join(merged))
        self.stored = True


class _QueryView:
    """The envelope metadata the store needs from a search request."""

    def __init__(self, query: str, lang: str | None, pageno: int):
        self.query = query
        self.lang = lang
        self.pageno = pageno


def request_context(search_query: "SearchQuery") -> RequestContext | None:
    """The cache context for a search request, or None when caching is
    disabled or the store is unavailable.

    Never raises: a broken cache must not break the search.
    """
    try:
        ctx = RequestContext(search_query)
    except Exception:  # pylint: disable=broad-except
        log.warning("serp-cache: context setup failed; running uncached", exc_info=True)
        return None
    if ctx.cache is None:
        return None
    return ctx
