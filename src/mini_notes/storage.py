"""Private, bounded local state. No credentials or telemetry are stored."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from platformdirs import user_data_path


MIB = 1024 * 1024
CACHE_LIMIT_BYTES = 20 * MIB
CACHE_LIMIT_ENTRIES = 64
SESSION_LIMIT_BYTES = 4 * MIB
PREFERENCES_LIMIT_BYTES = 64 * 1024
DATABASE_LIMIT_BYTES = 32 * MIB
CACHE_KEY_LIMIT_BYTES = 4096


class StorageError(RuntimeError):
    """Safe, actionable storage failure; never contains saved content or raw SQL."""

    def __init__(self, message: str, *, code: str = "storage_error", scope: str = "database"):
        super().__init__(message)
        self.code, self.scope = code, scope


class StorageLimitError(StorageError):
    def __init__(self, scope: str, actual_bytes: int, limit_bytes: int):
        label = {"session": "阅读会话", "preferences": "偏好设置",
                 "legacy_database": "旧阅读数据库", "database": "阅读数据库"}.get(scope, "缓存")
        super().__init__(f"{label}超过容量上限，旧保存记录已保留。",
                         code="storage_limit", scope=scope)
        self.actual_bytes, self.limit_bytes = actual_bytes, limit_bytes


class StateStore:
    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        max_entries: int = 64,
        max_bytes: int = 20 * 1024 * 1024,
        purge_cache: bool = False,
    ):
        self.data_dir = (
            Path(data_dir) if data_dir else user_data_path("mini-notes", appauthor=False)
        )
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data_dir / "reader.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self.max_entries = min(CACHE_LIMIT_ENTRIES, max(1, max_entries))
        self.max_bytes = min(CACHE_LIMIT_BYTES, max(1024, max_bytes))
        self.last_error: dict | None = None
        # A legacy file must not make SQLite silently raise max_page_count to its
        # existing size. Refuse before recovery/schema writes; never delete it.
        if self.path.exists() and self.path.stat().st_size > DATABASE_LIMIT_BYTES:
            raise StorageLimitError("legacy_database", self.path.stat().st_size,
                                    DATABASE_LIMIT_BYTES)
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        try:
            page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
            max_pages = DATABASE_LIMIT_BYTES // page_size
            actual_pages = self._db.execute(f"PRAGMA max_page_count={max_pages}").fetchone()[0]
            if actual_pages > max_pages:
                raise StorageLimitError("legacy_database", actual_pages * page_size,
                                        DATABASE_LIMIT_BYTES)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA journal_size_limit=0")
            self._db.execute("PRAGMA temp_store=MEMORY")
            self._db.execute("PRAGMA secure_delete=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            previous_version = self._db.execute("PRAGMA user_version").fetchone()[0]
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL,
                    touched REAL NOT NULL, bytes INTEGER NOT NULL
                );
            """)
            with self._db:
                if previous_version < 2:
                    self._db.execute("UPDATE cache SET bytes=length(CAST(value AS BLOB))")
                self._db.execute("DELETE FROM cache WHERE length(CAST(key AS BLOB))>?",
                                 (CACHE_KEY_LIMIT_BYTES,))
                self._evict_for(0, 0)
                self._db.execute("PRAGMA user_version=2")
        except (sqlite3.Error, StorageError) as exc:
            self._db.close()
            self._closed = True
            if isinstance(exc, StorageError):
                raise
            raise self._database_error(exc) from None
        if os.name != "nt":
            self.path.chmod(0o600)
        if purge_cache:
            self.compact_on_exit()

    @staticmethod
    def _decode(raw: str | None) -> dict:
        try:
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def load(self) -> dict:
        return self._read_value("session")

    def save(self, state: dict) -> None:
        # Serialize before opening the transaction: failure leaves the old session intact.
        self._save_value("session", self._serialize(state, "session", SESSION_LIMIT_BYTES))

    def get_preferences(self) -> dict:
        return self._read_value("preferences")

    def _read_value(self, key: str) -> dict:
        with self._lock:
            try:
                row = self._db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
                return self._decode(row[0] if row else None)
            except sqlite3.Error as exc:
                raise self._database_error(exc) from None

    def set_preferences(self, preferences: dict) -> None:
        self._save_value("preferences", self._serialize(
            preferences, "preferences", PREFERENCES_LIMIT_BYTES))

    def _record_error(self, exc: StorageError) -> StorageError:
        self.last_error = {"code": exc.code, "scope": exc.scope}
        if isinstance(exc, StorageLimitError):
            self.last_error.update(actual_bytes=exc.actual_bytes, limit_bytes=exc.limit_bytes)
        return exc

    def _database_error(self, exc: sqlite3.Error) -> StorageError:
        full = getattr(exc, "sqlite_errorcode", 0) == sqlite3.SQLITE_FULL
        return self._record_error(StorageError(
            "本地存储空间或数据库额度不足，旧保存记录已保留。" if full else
            "本地存储操作未完成，旧保存记录已保留。",
            code="storage_full" if full else "storage_error"))

    def _serialize(self, value: dict, scope: str, limit: int) -> str:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(text.encode("utf-8"))
        if size > limit:
            raise self._record_error(StorageLimitError(scope, size, limit))
        return text

    def _save_value(self, key: str, text: str) -> None:
        with self._lock:
            try:
                with self._db:
                    # Reuse freed pages instead of briefly keeping two large rows.
                    self._db.execute("DELETE FROM state WHERE key=?", (key,))
                    self._db.execute("INSERT INTO state(key,value) VALUES(?,?)", (key, text))
                self.last_error = None
            except sqlite3.Error as exc:
                raise self._database_error(exc) from None

    def _evict_for(self, incoming_bytes: int, incoming_entries: int) -> None:
        """Called within one transaction *before* inserting an optional cache row."""
        self._db.execute("DELETE FROM cache WHERE expires<=?", (time.time(),))
        while True:
            count, total = self._db.execute(
                "SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM cache").fetchone()
            if count + incoming_entries <= self.max_entries and total + incoming_bytes <= self.max_bytes:
                return
            self._db.execute(
                "DELETE FROM cache WHERE key=(SELECT key FROM cache ORDER BY touched,key LIMIT 1)")

    def cache_get(self, key: str) -> dict | None:
        now = time.time()
        with self._lock:
            try:
                row = self._db.execute(
                    "SELECT value,expires FROM cache WHERE key=?", (key,)).fetchone()
            except sqlite3.Error as exc:
                self._database_error(exc)
                return None
            if not row:
                return None
            expired = row[1] <= now
            try:
                with self._db:
                    if expired:
                        self._db.execute("DELETE FROM cache WHERE key=?", (key,))
                    else:
                        self._db.execute("UPDATE cache SET touched=? WHERE key=?", (now, key))
            except sqlite3.Error as exc:
                self._database_error(exc)
                # A failed optional LRU update does not invalidate readable text.
            return None if expired else self._decode(row[0])

    def cache_put(self, key: str, value: dict, ttl: float = 86400) -> bool:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(text.encode("utf-8"))
        if size > self.max_bytes or len(key.encode("utf-8")) > CACHE_KEY_LIMIT_BYTES:
            return False
        if ttl <= 0 or not math.isfinite(ttl):
            return False
        now = time.time()
        with self._lock:
            try:
                with self._db:
                    self._db.execute("DELETE FROM cache WHERE key=?", (key,))
                    self._evict_for(size, 1)
                    self._db.execute("INSERT INTO cache VALUES(?,?,?,?,?)",
                                     (key, text, now + ttl, now, size))
                return True
            except sqlite3.Error as exc:
                self._database_error(exc)
                return False  # Optional preparation must never replace a valid session.

    def cache_info(self) -> dict[str, Any]:
        with self._lock:
            try:
                count, size = self._db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM cache"
                ).fetchone()
            except sqlite3.Error as exc:
                raise self._database_error(exc) from None
            return {
                "entries": count,
                "bytes": size,
                "limit_entries": self.max_entries,
                "limit_bytes": self.max_bytes,
            }

    def clear_cache(self) -> None:
        """Clear session-only bodies, retaining the separately saved resume state."""
        with self._lock:
            try:
                with self._db:
                    self._db.execute("DELETE FROM cache")
            except sqlite3.Error as exc:
                raise self._database_error(exc) from None

    def storage_info(self) -> dict[str, Any]:
        """Metadata only; scoped to this DB, not installation or other sources."""
        with self._lock:
            try:
                return self._storage_info()
            except sqlite3.Error as exc:
                raise self._database_error(exc) from None
            except OSError:
                raise self._record_error(StorageError("无法读取本地存储大小；原记录未修改。")) from None

    def _storage_info(self) -> dict[str, Any]:
        with self._lock:
            files = {}
            for name, suffix in (("db", ""), ("journal", "-journal"),
                                 ("wal", "-wal"), ("shm", "-shm")):
                try:
                    files[name + "_bytes"] = Path(str(self.path) + suffix).stat().st_size
                except FileNotFoundError:
                    files[name + "_bytes"] = 0
            sizes = dict(self._db.execute(
                "SELECT key,length(CAST(value AS BLOB)) FROM state WHERE key IN ('session','preferences')"
            ).fetchall())
            cache = self.cache_info()
            limits = dict(cache_bytes=self.max_bytes, cache_entries=self.max_entries,
                          session_bytes=SESSION_LIMIT_BYTES,
                          preferences_bytes=PREFERENCES_LIMIT_BYTES,
                          database_bytes=DATABASE_LIMIT_BYTES)
            return {
                **files, "managed_file_bytes": sum(files.values()),
                "cache_payload_bytes": cache["bytes"], "cache_entries": cache["entries"],
                "session_bytes": sizes.get("session", 0),
                "preferences_bytes": sizes.get("preferences", 0), "limits": limits,
                "journal_mode": self._db.execute("PRAGMA journal_mode").fetchone()[0],
                "temp_store": "memory" if self._db.execute("PRAGMA temp_store").fetchone()[0] == 2 else "file",
                "last_error": dict(self.last_error) if self.last_error else None,
                "legacy_oversized": [key for key, limit in (("session", SESSION_LIMIT_BYTES),
                    ("preferences", PREFERENCES_LIMIT_BYTES)) if sizes.get(key, 0) > limit],
            }

    def compact_on_exit(self, state: dict | None = None) -> None:
        # Serialize before deleting anything; a bad snapshot must not lose valid state.
        text = self._serialize(state, "session", SESSION_LIMIT_BYTES) if state is not None else None
        with self._lock:
            if self._closed:
                return
            try:
                with self._db:
                    if text is not None:
                        self._db.execute("DELETE FROM state WHERE key='session'")
                        self._db.execute("INSERT INTO state VALUES('session',?)", (text,))
                    self._db.execute("DELETE FROM cache")
                # VACUUM may need up to 2x the DB size in additional disk space;
                # journal_size_limit only limits retained journals after commit.
                # Neither operation guarantees erasure from snapshots or SSDs.
                self._db.execute("VACUUM")
                self.last_error = None
            except sqlite3.Error as exc:
                raise self._database_error(exc) from None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True


class SessionInUseError(RuntimeError):
    pass


class SessionLock:
    """An OS-owned lock, automatically released even after process termination."""

    def __init__(self, data_dir: str | Path | None = None):
        directory = Path(data_dir) if data_dir else user_data_path("mini-notes", appauthor=False)
        self.path = directory / ".session.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._file = open(self.path, "a+b")
        if os.name != "nt":
            self.path.chmod(0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if self._file.seek(0, 2) == 0:
                    self._file.write(b"0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            self._file.close()
            self._file = None
            raise SessionInUseError(
                "已有窗口正在使用此阅读目录；请先退出，或用 --data-dir 选择另一目录。"
            ) from exc

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
