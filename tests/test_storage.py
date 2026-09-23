import os
import time

import pytest

from mini_notes.storage import StateStore


def test_session_survives_reopen_and_cache_is_bounded(tmp_path):
    s = StateStore(tmp_path, max_entries=2)
    s.save({"position": {"paragraph": 5}, "query": "雨"})
    s.cache_put("a", {"body": "甲"})
    s.cache_put("b", {"body": "乙"})
    assert s.cache_get("a") == {"body": "甲"}
    s.cache_put("c", {"body": "丙"})
    assert s.cache_get("b") is None
    s.close()
    s = StateStore(tmp_path)
    assert s.load()["position"]["paragraph"] == 5
    assert s.load()["query"] == "雨"
    if os.name != "nt":
        assert s.path.stat().st_mode & 0o777 == 0o600
    s.close()


def test_failed_save_keeps_previous_valid_state(tmp_path):
    s = StateStore(tmp_path)
    s.save({"valid": True})
    with pytest.raises(ValueError):
        s.save({"bad": float("nan")})
    assert s.load() == {"valid": True}
    s.close()


def test_expired_and_oversize_cache_are_not_returned(tmp_path, monkeypatch):
    s = StateStore(tmp_path, max_bytes=1024)
    now = time.time()
    s.cache_put("short", {"text": "ok"}, ttl=1)
    s.cache_put("large", {"text": "x" * 2000})
    monkeypatch.setattr(time, "time", lambda: now + 2)
    assert s.cache_get("short") is None
    assert s.cache_get("large") is None
    s.close()


def test_exit_compacts_current_state_and_startup_purges_unclean_cache(tmp_path):
    store = StateStore(tmp_path)
    store.save({"current": {"id": "original"}})
    store.cache_put("preload", {"paragraphs": ["原创预取内容" * 1000]})
    before = store.path.stat().st_size
    store.compact_on_exit({"current": {"id": "latest"}, "position": {"paragraph": 4}})
    assert store.cache_info()["entries"] == 0
    assert store.path.stat().st_size < before
    assert store._db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    store.cache_put("unclean", {"body": "尚未清除的缓存"})
    store.close()
    store = StateStore(tmp_path, purge_cache=True)
    assert store.cache_info()["entries"] == 0
    assert store.load()["current"]["id"] == "latest"
    assert store.load()["position"] == {"paragraph": 4}
    store.close()


def test_invalid_compaction_snapshot_retains_state_and_cache(tmp_path):
    store = StateStore(tmp_path)
    store.save({"valid": True})
    store.cache_put("valid", {"body": "有效缓存"})
    with pytest.raises(ValueError):
        store.compact_on_exit({"invalid": float("nan")})
    assert store.load() == {"valid": True}
    assert store.cache_get("valid") == {"body": "有效缓存"}
    store.close()


def test_session_lock_prevents_second_process_and_releases_on_process_exit(tmp_path):
    import subprocess
    import sys
    from mini_notes.storage import SessionInUseError, SessionLock

    lock = SessionLock(tmp_path)
    with pytest.raises(SessionInUseError):
        SessionLock(tmp_path)
    probe = (
        "from mini_notes.storage import SessionLock; import sys; "
        "lock=SessionLock(sys.argv[1]); print('locked',flush=True); sys.stdin.readline()"
    )
    denied = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)], capture_output=True, text=True, input="\n"
    )
    assert denied.returncode != 0 and "SessionInUseError" in denied.stderr
    lock.close()
    child = subprocess.Popen(
        [sys.executable, "-c", probe, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(SessionInUseError):
            SessionLock(tmp_path)
    finally:
        child.terminate()
        child.wait(timeout=5)
    SessionLock(tmp_path).close()
