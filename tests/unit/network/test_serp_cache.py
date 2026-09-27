# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the persistent SERP cache (searx.network.serp_cache)."""

# pylint: disable=missing-module-docstring, protected-access

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from searx.network import serp_cache


def _query(query="lorem ipsum", lang="en", pageno=1, safesearch=0, time_range=None, engines=None):
    """A minimal stand-in for SearchQuery (only the key fields)."""
    enginerefs = engines if engines is not None else [("google", "general"), ("bing", "general")]
    return SimpleNamespace(
        query=query,
        lang=lang,
        pageno=pageno,
        safesearch=safesearch,
        time_range=time_range,
        engineref_list=[SimpleNamespace(name=n, category=c) for n, c in enginerefs],
    )


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    """A SerpCache on a temp dir with a long TTL."""
    monkeypatch.setattr(serp_cache, "_ttl_seconds", lambda: 3600.0)
    return serp_cache.SerpCache(str(tmp_path / "serp_cache.sqlite3"))


class TestCanonicalKey:
    def test_same_identity_same_key(self):
        assert serp_cache.canonical_key(_query()) == serp_cache.canonical_key(_query())

    def test_whitespace_normalized_query_shares_key(self):
        assert serp_cache.canonical_key(_query("lorem   ipsum")) == serp_cache.canonical_key(_query("lorem ipsum"))

    def test_locale_changes_key(self):
        assert serp_cache.canonical_key(_query(lang="en")) != serp_cache.canonical_key(_query(lang="de"))

    def test_pageno_changes_key(self):
        assert serp_cache.canonical_key(_query(pageno=1)) != serp_cache.canonical_key(_query(pageno=2))

    def test_safesearch_changes_key(self):
        assert serp_cache.canonical_key(_query(safesearch=0)) != serp_cache.canonical_key(_query(safesearch=1))

    def test_time_range_changes_key(self):
        assert serp_cache.canonical_key(_query(time_range=None)) != serp_cache.canonical_key(_query(time_range="day"))

    def test_engine_set_changes_key(self):
        base = [("google", "general")]
        assert serp_cache.canonical_key(_query(engines=base)) != serp_cache.canonical_key(
            _query(engines=[("google", "general"), ("bing", "general")])
        )

    def test_category_changes_key(self):
        assert serp_cache.canonical_key(_query(engines=[("google", "general")])) != serp_cache.canonical_key(
            _query(engines=[("google", "images")])
        )

    def test_key_is_short_hex(self):
        key = serp_cache.canonical_key(_query())
        assert len(key) == 32
        int(key, 16)


class TestStoreLookup:
    def test_round_trip(self, cache):
        query = _query()
        entry = serp_cache._CacheEntry(
            serp_cache.canonical_key(query), query, {"google": [{"__type": "legacy", "url": "https://a.example"}]}
        )
        cache.store(entry)
        loaded = cache.lookup(entry.key)
        assert loaded is not None
        assert loaded.blocks["google"] == entry.blocks["google"]

    def test_miss_on_unknown_key(self, cache):
        assert cache.lookup(uuid4().hex) is None

    def test_expired_entry_is_a_miss(self, cache, monkeypatch):
        query = _query()
        entry = serp_cache._CacheEntry(serp_cache.canonical_key(query), query, {"google": [{"url": "x"}]})
        entry.expires_at = time.time() - 1
        cache.store(entry)
        assert cache.lookup(entry.key) is None

    def test_ttl_is_set_on_write_only(self, cache):
        """Reads never extend the expiry: a fresh lookup of a stale entry
        still reports the original expiry instant."""
        query = _query()
        entry = serp_cache._CacheEntry(serp_cache.canonical_key(query), query, {"google": [{"url": "x"}]})
        cache.store(entry)
        original_expiry = entry.expires_at
        time.sleep(0.01)
        loaded = cache.lookup(entry.key)
        assert loaded.expires_at == original_expiry

    def test_partial_update_keeps_original_expiry(self, cache):
        query = _query()
        entry = serp_cache._CacheEntry(serp_cache.canonical_key(query), query, {"google": [{"url": "x"}]})
        cache.store(entry)
        merged = dict(entry.blocks)
        merged["bing"] = [{"url": "y"}]
        updated = serp_cache._CacheEntry(entry.key, query, merged)
        updated.created_at = entry.created_at
        updated.expires_at = entry.expires_at
        cache.store(updated)
        loaded = cache.lookup(entry.key)
        assert set(loaded.blocks) == {"google", "bing"}
        assert loaded.expires_at == entry.expires_at

    def test_prune_drops_expired(self, cache):
        query = _query()
        entry = serp_cache._CacheEntry(serp_cache.canonical_key(query), query, {"google": [{"url": "x"}]})
        entry.expires_at = time.time() - 1
        cache.store(entry)
        cache.prune()
        assert cache.lookup(entry.key) is None

    def test_payload_is_zstd(self, cache):
        """The stored blob must be zstd frames, not plaintext."""
        import zstandard as zstd  # pylint: disable=import-outside-toplevel

        query = _query()
        entry = serp_cache._CacheEntry(serp_cache.canonical_key(query), query, {"google": [{"url": "x" * 500}]})
        cache.store(entry)
        row = cache._conn.execute("SELECT payload FROM serp_cache WHERE key = ?", (entry.key,)).fetchone()
        magic = b"\x28\xb5\x2f\xfd"  # zstd frame magic
        assert row[0][:4] == magic
        assert b"xxxxxxxxxx" not in row[0]


class TestResultSerialization:
    def test_struct_round_trip(self):
        from searx.result_types import Image, LegacyResult  # pylint: disable=import-outside-toplevel

        image = Image(title="a picture", url="https://a.example/img.png", img_src="https://a.example/img.png")
        dumped = serp_cache.dump_results([image])
        loaded = serp_cache.load_results(dumped)
        assert isinstance(loaded[0], Image)
        assert loaded[0].url == "https://a.example/img.png"

    def test_legacy_dict_round_trip(self):
        from searx.result_types import LegacyResult  # pylint: disable=import-outside-toplevel

        dumped = serp_cache.dump_results([{"url": "https://a.example", "title": "t"}])
        loaded = serp_cache.load_results(dumped)
        assert isinstance(loaded[0], LegacyResult)
        assert loaded[0]["title"] == "t"

    def test_unrebuildable_type_degrades_to_legacy(self):
        dumped = [{"__type": "NoSuchType", "url": "https://a.example"}]
        loaded = serp_cache.load_results(dumped)
        assert loaded[0]["url"] == "https://a.example"


class TestRequestContext:
    def test_disabled_cache_returns_none(self, monkeypatch):
        monkeypatch.setattr(serp_cache, "enabled", lambda: False)
        assert serp_cache.request_context(_query()) is None

    def test_full_hit_replays_everything(self, cache, monkeypatch):
        monkeypatch.setattr(serp_cache, "get_cache", lambda: cache)
        query = _query()
        key = serp_cache.canonical_key(query)
        entry = serp_cache._CacheEntry(
            key,
            query,
            {
                "google": [{"__type": "legacy", "url": "https://a.example"}],
                "bing": [{"__type": "legacy", "url": "https://b.example"}],
            },
        )
        cache.store(entry)

        ctx = serp_cache.request_context(query)
        container = SimpleNamespace(serp_cache_blocks=None, extend=lambda name, results: None)
        requests = [("google", "lorem ipsum", {}), ("bing", "lorem ipsum", {})]
        remaining = ctx.apply(container, requests)
        assert remaining == []
        ctx.finish(container)
        assert ctx.stored

    def test_partial_hit_fetches_missing_engines(self, cache, monkeypatch):
        monkeypatch.setattr(serp_cache, "get_cache", lambda: cache)
        query = _query()
        key = serp_cache.canonical_key(query)
        entry = serp_cache._CacheEntry(key, query, {"google": [{"__type": "legacy", "url": "https://a.example"}]})
        cache.store(entry)

        ctx = serp_cache.request_context(query)
        container = SimpleNamespace(serp_cache_blocks=None, extend=lambda name, results: None)
        requests = [("google", "lorem ipsum", {}), ("bing", "lorem ipsum", {})]
        remaining = ctx.apply(container, requests)
        assert [r[0] for r in remaining] == ["bing"]

        # the fan-out produced the missing engine's block; finish merges it
        container.serp_cache_blocks = {"bing": [{"url": "https://b.example"}]}
        ctx.finish(container)
        merged = cache.lookup(key)
        assert set(merged.blocks) == {"google", "bing"}

    def test_miss_writes_entry_on_finish(self, cache, monkeypatch):
        monkeypatch.setattr(serp_cache, "get_cache", lambda: cache)
        from searx.result_types import Image  # pylint: disable=import-outside-toplevel

        query = _query()
        key = serp_cache.canonical_key(query)

        ctx = serp_cache.request_context(query)
        container = SimpleNamespace(serp_cache_blocks=None, extend=lambda name, results: None)
        requests = [("google", "lorem ipsum", {})]
        assert [r[0] for r in ctx.apply(container, requests)] == ["google"]

        # production stores RAW result objects (msgspec structs), not dicts
        raw = Image(title="a", url="https://a.example", img_src="https://a.example")
        container.serp_cache_blocks = {"google": [raw]}
        ctx.finish(container)
        loaded = cache.lookup(key)
        assert loaded.blocks["google"]
        assert loaded.blocks["google"][0]["__type"] == "Image"

    def test_empty_blocks_are_not_cached(self, cache, monkeypatch):
        monkeypatch.setattr(serp_cache, "get_cache", lambda: cache)
        query = _query()
        key = serp_cache.canonical_key(query)

        ctx = serp_cache.request_context(query)
        container = SimpleNamespace(serp_cache_blocks=None, extend=lambda name, results: None)
        ctx.apply(container, [("google", "lorem ipsum", {})])
        container.serp_cache_blocks = {"google": []}
        ctx.finish(container)
        assert cache.lookup(key) is None

    def test_broken_cache_never_breaks_the_search(self, monkeypatch, tmp_path):
        """An unreadable store path degrades to no caching, not to errors."""
        monkeypatch.setattr(serp_cache, "enabled", lambda: True)
        monkeypatch.setattr(serp_cache, "_cache", None)
        # a directory cannot be opened as a SQLite database file
        blocker = tmp_path / "serp_cache.sqlite3"
        blocker.mkdir()
        monkeypatch.setattr(serp_cache, "get_setting", lambda name, default=None: str(blocker))
        assert serp_cache.request_context(_query()) is None


class TestGetSettingIntegration:
    def test_defaults_are_sane(self, monkeypatch):
        monkeypatch.setattr(serp_cache, "get_setting", lambda name, default=None: default)
        assert serp_cache._ttl_seconds() == 24 * 3600
        assert serp_cache._prune_interval() == 50
        assert serp_cache._compression_level() == 3
