"""Capacity contracts use only original synthetic text in isolated directories."""

import json
import sqlite3
import time

import pytest

from mini_notes import storage


def test_public_limits_cannot_be_raised_and_info_reports_actual_files(tmp_path):
    store = storage.StateStore(tmp_path, max_entries=1000, max_bytes=100 * 1024 * 1024)
    assert store.max_entries == 64
    assert store.max_bytes == 20 * 1024 * 1024
    info = store.storage_info()
    assert info["db_bytes"] == store.path.stat().st_size
    assert info["managed_file_bytes"] == info["db_bytes"]
    assert info["limits"]["database_bytes"] == 32 * 1024 * 1024
    assert info["journal_mode"] == "delete" and info["temp_store"] == "memory"
    pages = store._db.execute("PRAGMA max_page_count").fetchone()[0]
    size = store._db.execute("PRAGMA page_size").fetchone()[0]
    assert pages * size <= info["limits"]["database_bytes"]
    store.close()


def test_session_and_preferences_utf8_limits_preserve_old_values(tmp_path):
    store = storage.StateStore(tmp_path)
    store.save({"current": "原创正文", "position": 7})
    store.set_preferences({"theme": "dark"})
    with pytest.raises(Exception, match="会话") as error:
        store.save({"current": "甲" * (4 * 1024 * 1024 // 3)})
    assert error.value.code == "storage_limit" and error.value.scope == "session"
    assert store.load() == {"current": "原创正文", "position": 7}
    with pytest.raises(Exception, match="偏好"):
        store.set_preferences({"theme": "乙" * (64 * 1024 // 3)})
    assert store.get_preferences() == {"theme": "dark"}
    store.close()


def test_oversized_compaction_preserves_state_and_cache(tmp_path):
    store = storage.StateStore(tmp_path)
    store.save({"current": "旧记录"})
    store.cache_put("cached", {"text": "原创缓存"})
    with pytest.raises(Exception, match="会话"):
        store.compact_on_exit({"current": "x" * (4 * 1024 * 1024)})
    assert store.load() == {"current": "旧记录"}
    assert store.cache_get("cached") == {"text": "原创缓存"}
    store.close()


def test_lru_evicts_before_insert_including_replacement(tmp_path):
    store = storage.StateStore(tmp_path, max_bytes=1024, max_entries=2)
    # A trigger observes the insertion boundary, not just the final table size.
    store._db.executescript("""
        CREATE TRIGGER preinsert_budget BEFORE INSERT ON cache
        WHEN (SELECT COALESCE(SUM(bytes),0) FROM cache) + NEW.bytes > 1024
          OR (SELECT COUNT(*) FROM cache) >= 2
        BEGIN SELECT RAISE(ABORT,'cache transient overflow'); END;
    """)
    assert store.cache_put("a", {"text": "a" * 450}) is True
    assert store.cache_put("b", {"text": "b" * 450}) is True
    store.cache_get("a")
    assert store.cache_put("c", {"text": "c" * 450}) is True
    assert store.cache_get("b") is None
    assert store.cache_put("a", {"text": "replacement" * 70}) is True
    assert store.cache_info()["bytes"] <= 1024
    assert store.cache_get("a") is not None
    store.close()


def test_large_cache_value_or_key_is_rejected_without_touching_session(tmp_path):
    store = storage.StateStore(tmp_path, max_bytes=1024)
    store.save({"current": "保持"})
    assert store.cache_put("x" * 4097, {"text": "small"}) is False
    assert store.cache_put("large", {"text": "x" * 1024}) is False
    assert store.cache_info()["entries"] == 0
    assert store.load() == {"current": "保持"}
    store.close()


def test_sqlite_full_cache_is_optional_and_session_write_is_controlled(tmp_path):
    store = storage.StateStore(tmp_path)
    store.save({"current": "previous"})
    existing_pages = store._db.execute("PRAGMA page_count").fetchone()[0]
    store._db.execute(f"PRAGMA max_page_count={existing_pages + 1}")
    assert store.cache_put("large", {"text": "x" * 100000}) is False
    with pytest.raises(storage.StorageError) as error:
        store.save({"current": "x" * 100000})
    assert error.value.code == "storage_full"
    assert store.load() == {"current": "previous"}
    assert store.path.stat().st_size <= (existing_pages + 1) * 4096
    store.close()


def test_clear_and_vacuum_preserve_current_and_preferences(tmp_path):
    store = storage.StateStore(tmp_path)
    current = {"current": {"paragraphs": ["原创当前段落"]}, "position": 4}
    store.save(current)
    store.set_preferences({"source": "official", "theme": "light"})
    for index in range(8):
        store.cache_put(str(index), {"text": "原创压力材料" * 10000})
    before = store.path.stat().st_size
    store.clear_cache()
    assert store.load() == current and store.get_preferences()["theme"] == "light"
    store.compact_on_exit()
    info = store.storage_info()
    assert info["db_bytes"] < before and info["cache_entries"] == 0
    assert info["journal_bytes"] == info["wal_bytes"] == info["shm_bytes"] == 0
    store.close()


def test_v1_migration_recounts_and_trims_cache_without_changing_state(tmp_path):
    path = tmp_path / "reader.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE cache(key TEXT PRIMARY KEY,value TEXT NOT NULL,expires REAL NOT NULL,
                           touched REAL NOT NULL,bytes INTEGER NOT NULL);
        PRAGMA user_version=1;
    """)
    original = {"current": "原创迁移正文", "position": 19}
    db.execute("INSERT INTO state VALUES('session',?)", (json.dumps(original),))
    for index in range(5):
        db.execute("INSERT INTO cache VALUES(?,?,?,?,?)",
                   (str(index), json.dumps({"text": "q" * 500}), time.time()+99, index, 1))
    db.commit()
    db.close()
    store = storage.StateStore(tmp_path, max_entries=2, max_bytes=1024)
    assert store.load() == original
    assert store.cache_info()["entries"] <= 2 and store.cache_info()["bytes"] <= 1024
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 2
    store.close()


def test_oversized_legacy_file_is_refused_before_any_write(tmp_path):
    path = tmp_path / "reader.sqlite3"
    with path.open("wb") as handle:
        handle.truncate(32 * 1024 * 1024 + 4096)
    before = path.stat()
    with pytest.raises(storage.StorageLimitError) as error:
        storage.StateStore(tmp_path)
    assert error.value.scope == "legacy_database"
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_legacy_oversized_session_is_kept_until_explicit_valid_replacement(tmp_path):
    store = storage.StateStore(tmp_path)
    legacy = json.dumps({"current": "x" * (4 * 1024 * 1024)})
    store._db.execute("INSERT OR REPLACE INTO state VALUES('session',?)", (legacy,))
    store._db.commit()
    store.close()
    store = storage.StateStore(tmp_path)
    assert "session" in store.storage_info()["legacy_oversized"]
    assert len(store.load()["current"]) == 4 * 1024 * 1024
    store.save({"current": "明确替换后的完整正文"})
    assert store.storage_info()["legacy_oversized"] == []
    store.close()


@pytest.mark.parametrize("page_size", [1024, 8192])
def test_database_page_limit_tracks_legacy_page_size_and_survives_vacuum(tmp_path, page_size):
    db = sqlite3.connect(tmp_path / "reader.sqlite3")
    db.execute(f"PRAGMA page_size={page_size}")
    db.execute("CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.commit()
    db.close()
    store = storage.StateStore(tmp_path)
    assert store._db.execute("PRAGMA page_size").fetchone()[0] == page_size
    assert store._db.execute("PRAGMA max_page_count").fetchone()[0] * page_size <= 32 * 1024 * 1024
    store.compact_on_exit()
    assert store._db.execute("PRAGMA max_page_count").fetchone()[0] * page_size <= 32 * 1024 * 1024
    store.close()


def test_hard_page_cap_rejects_growth_even_outside_logical_cache_budget(tmp_path):
    store = storage.StateStore(tmp_path)
    store.save({"current": "保留的原创正文"})
    # Exercise SQLite's independent physical guard; this bypass is only a test.
    with pytest.raises(sqlite3.OperationalError) as error:
        with store._db:
            store._db.execute("INSERT INTO state VALUES('synthetic-test',zeroblob(?))",
                              (33 * 1024 * 1024,))
    assert error.value.sqlite_errorcode == sqlite3.SQLITE_FULL
    assert store.path.stat().st_size <= 32 * 1024 * 1024
    assert store.load() == {"current": "保留的原创正文"}
    assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.close()


def test_broken_optional_cache_is_a_miss_and_does_not_lose_saved_current(tmp_path):
    store = storage.StateStore(tmp_path)
    store.save({"current": "原创已保存内容"})
    store._db.execute("DROP TABLE cache")
    assert store.cache_get("missing") is None
    assert store.last_error["code"] == "storage_error"
    assert store.load() == {"current": "原创已保存内容"}
    store.close()


def test_readonly_cache_can_return_content_when_lru_touch_fails(tmp_path):
    store = storage.StateStore(tmp_path)
    original = {"text": "原创只读缓存"}
    store.cache_put("readable", original)
    store._db.execute("PRAGMA query_only=ON")
    assert store.cache_get("readable") == original
    assert store.last_error["code"] == "storage_error"
    store.close()


@pytest.mark.parametrize("method", ["load", "get_preferences", "cache_info", "storage_info"])
def test_required_reads_translate_sqlite_errors_without_raw_details(tmp_path, method):
    store = storage.StateStore(tmp_path)
    store._db.close()
    with pytest.raises(storage.StorageError) as error:
        getattr(store, method)()
    assert error.value.code == "storage_error"
    assert "closed database" not in str(error.value)
    store.close()
