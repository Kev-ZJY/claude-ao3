"""Bound the app-owned stores across explicitly configured source namespaces.

Called only while the CLI holds the data-root SessionLock. Files created by the
user/other programs, the Python runtime and the installed environment are separate.
"""
from __future__ import annotations

from pathlib import Path
import re

MIB = 1024 * 1024
DATABASE_RESERVATION = 32 * MIB
PERSISTENT_BUDGET = 96 * MIB
TRANSIENT_RESERVATION = 65 * MIB  # VACUUM allowance plus journal/page-header margin.
NAMESPACE = re.compile(r"[0-9a-f]{16}\Z")


class StorageBudgetError(RuntimeError):
    pass


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except FileNotFoundError:
        return 0  # A completed transaction may just have removed its journal.


def _managed_databases(root: Path) -> list[Path]:
    databases = [root / "reader.sqlite3"]
    sources = root / "sources"
    if sources.is_symlink():
        raise StorageBudgetError("阅读数据的sources目录不能是符号链接；请使用独立数据目录。")
    if sources.exists():
        for directory in sources.iterdir():
            if NAMESPACE.fullmatch(directory.name):
                if directory.is_symlink():
                    raise StorageBudgetError("书源数据目录不能是符号链接；未修改已有数据。")
                if directory.is_dir():
                    databases.append(directory / "reader.sqlite3")
    return databases


def reading_storage_usage(data_root: str | Path) -> dict:
    root = Path(data_root)
    databases = _managed_databases(root)
    persistent = auxiliary = count = 0
    for database in databases:
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(database) + suffix)
            if path.is_symlink():
                raise StorageBudgetError("阅读数据库不能是符号链接；未修改已有数据。")
            size = _file_size(path)
            if size:
                if suffix:
                    auxiliary += size
                else:
                    persistent += size
                    count += 1
    return {
        "database_bytes": persistent,
        "auxiliary_bytes": auxiliary,
        "managed_file_bytes": persistent + auxiliary,
        "database_count": count,
        "persistent_budget_bytes": PERSISTENT_BUDGET,
        "transient_reservation_bytes": TRANSIENT_RESERVATION,
        "managed_budget_bytes": PERSISTENT_BUDGET + TRANSIENT_RESERVATION,
    }


def check_settings_budget(data_root: str | Path) -> dict:
    """Preflight the settings store before it is opened or migrated."""
    root = Path(data_root)
    usage = reading_storage_usage(root)
    projected = usage["managed_file_bytes"] - _file_size(root / "reader.sqlite3") + DATABASE_RESERVATION
    if projected > PERSISTENT_BUDGET:
        raise StorageBudgetError("阅读数据已超过容量预算，未打开或迁移数据库；已有进度未删除。")
    return {**usage, "reserved_persistent_bytes": projected}


def check_storage_budget(data_root: str | Path, namespace: str) -> dict:
    """Reserve both open stores' maximum size, never delete old source progress."""
    if not NAMESPACE.fullmatch(namespace):
        raise ValueError("Invalid source namespace")
    root = Path(data_root)
    usage = reading_storage_usage(root)
    settings = root / "reader.sqlite3"
    active = root / "sources" / namespace / "reader.sqlite3"
    current_sizes = sum(_file_size(p) for p in (settings, active))
    # Retained journals of other sources can coexist with a new active transaction.
    # Count all existing auxiliary files conservatively instead of ignoring them.
    projected = usage["database_bytes"] + usage["auxiliary_bytes"] - current_sizes + 2 * DATABASE_RESERVATION
    if projected > PERSISTENT_BUDGET or usage["managed_file_bytes"] > PERSISTENT_BUDGET + TRANSIENT_RESERVATION:
        raise StorageBudgetError(
            "阅读数据空间不足：无法为当前书源保留容量。已有进度未删除；"
            "请整理不用的旧书源目录，或用 --data-dir 指定其他目录。"
        )
    return {**usage, "reserved_persistent_bytes": projected}
