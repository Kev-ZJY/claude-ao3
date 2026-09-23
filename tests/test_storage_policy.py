from pathlib import Path

import pytest

from mini_notes.storage_policy import (
    DATABASE_RESERVATION, PERSISTENT_BUDGET, StorageBudgetError,
    check_storage_budget, reading_storage_usage,
)


def sparse(path: Path, size: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as stream:
        stream.truncate(size)


def test_reserves_active_and_settings_before_new_source_exists(tmp_path):
    sparse(tmp_path/'sources'/('b'*16)/'reader.sqlite3', 32*1024*1024)
    data = check_storage_budget(tmp_path, 'a'*16)
    assert data['reserved_persistent_bytes'] == PERSISTENT_BUDGET
    assert not (tmp_path/'sources'/('a'*16)).exists()


def test_old_sources_cannot_accumulate_beyond_global_reserved_budget(tmp_path):
    old = tmp_path/'sources'/('b'*16)/'reader.sqlite3'
    sparse(old, DATABASE_RESERVATION+1)
    with pytest.raises(StorageBudgetError, match='已有进度未删除'):
        check_storage_budget(tmp_path, 'a'*16)
    assert old.stat().st_size == DATABASE_RESERVATION+1
    assert not (tmp_path/'sources'/('a'*16)).exists()


def test_active_existing_database_is_counted_as_reservation_not_twice(tmp_path):
    sparse(tmp_path/'reader.sqlite3', 1024)
    sparse(tmp_path/'sources'/('a'*16)/'reader.sqlite3', DATABASE_RESERVATION)
    sparse(tmp_path/'sources'/('b'*16)/'reader.sqlite3', DATABASE_RESERVATION)
    assert check_storage_budget(tmp_path, 'a'*16)['reserved_persistent_bytes'] == PERSISTENT_BUDGET


def test_usage_counts_journal_but_does_not_inspect_content_or_unowned_files(tmp_path):
    path=tmp_path/'sources'/('a'*16)/'reader.sqlite3'
    sparse(path, 1000)
    sparse(Path(str(path)+'-journal'), 500)
    sparse(tmp_path/'unrelated.bin', 9999)
    assert reading_storage_usage(tmp_path)['managed_file_bytes'] == 1500


def test_namespace_symlink_cannot_bypass_quota(tmp_path):
    external=tmp_path/'external'
    external.mkdir()
    (tmp_path/'sources').mkdir()
    (tmp_path/'sources'/('a'*16)).symlink_to(external, target_is_directory=True)
    with pytest.raises(StorageBudgetError):
        check_storage_budget(tmp_path, 'a'*16)


def test_retained_rollback_journal_cannot_bypass_multi_source_budget(tmp_path):
    old = tmp_path/'sources'/('b'*16)/'reader.sqlite3'
    sparse(old, DATABASE_RESERVATION)
    journal=Path(str(old)+'-journal')
    sparse(journal, DATABASE_RESERVATION)
    with pytest.raises(StorageBudgetError):
        check_storage_budget(tmp_path, 'a'*16)
    assert journal.stat().st_size == DATABASE_RESERVATION
