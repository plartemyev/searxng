# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the SQLite expire cache (searx.cache)"""

# pylint: disable=missing-function-docstring,missing-module-docstring
# pylint: disable=redefined-outer-name,protected-access,unused-argument

import sqlite3

import pytest

from searx.cache import ExpireCacheCfg, ExpireCacheSQLite


@pytest.fixture()
def cache(tmp_path):
    # note: a name with a '-' breaks the unquoted CREATE TABLE (pre-existing
    # upstream quirk); real caches use names like ENGINES_CACHE
    cfg = ExpireCacheCfg(name="unit_test_cache", db_url=str(tmp_path / "test_cache.db"))
    return ExpireCacheSQLite(cfg)


# _setmany: a transient busy/locked DB must not fail the engine request


def test_cache_set_retries_on_busy_lock(cache, monkeypatch):
    real_setmany_once = cache._setmany_once
    calls = {"n": 0}

    def busy_then_ok(opt_list, ctx=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_setmany_once(opt_list=opt_list, ctx=ctx)

    monkeypatch.setattr(cache, "_setmany_once", busy_then_ok)
    assert cache.set("key", "value", expire=3600) is True
    assert calls["n"] == 2
    assert cache.get("key") == "value"


def test_cache_set_gives_up_after_the_last_attempt(cache, monkeypatch):
    calls = {"n": 0}

    def always_busy(opt_list, ctx=None):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cache, "_setmany_once", always_busy)
    with pytest.raises(sqlite3.OperationalError):
        cache.set("key", "value", expire=3600)
    assert calls["n"] == 3


def test_cache_set_does_not_retry_unrelated_errors(cache, monkeypatch):
    calls = {"n": 0}

    def broken(opt_list, ctx=None):
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: whatever")

    monkeypatch.setattr(cache, "_setmany_once", broken)
    with pytest.raises(sqlite3.OperationalError):
        cache.set("key", "value", expire=3600)
    assert calls["n"] == 1


def test_is_busy_error_recognizes_transient_locks():
    assert ExpireCacheSQLite._is_busy_error(sqlite3.OperationalError("database is locked"))
    assert ExpireCacheSQLite._is_busy_error(sqlite3.OperationalError("database table is locked"))
    assert ExpireCacheSQLite._is_busy_error(sqlite3.OperationalError("database is busy"))
    assert not ExpireCacheSQLite._is_busy_error(sqlite3.OperationalError("no such table: x"))
    assert not ExpireCacheSQLite._is_busy_error(sqlite3.OperationalError("disk I/O error"))


# plain set/get roundtrip (the paths above must not have broken it)


def test_cache_set_get_roundtrip(cache):
    assert cache.set("alpha", {"deep": 1}, expire=3600) is True
    assert cache.get("alpha") == {"deep": 1}
    assert cache.get("missing") is None
