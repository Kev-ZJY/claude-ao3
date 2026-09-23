"""Persistence and clear-preparation acceptance using original source fixtures."""

import asyncio
import copy
import json

from mini_notes.engine import ReaderEngine
from mini_notes.models import Page, SearchFilters
from test_engine import FixtureSource, MemoryStore, engine, fixture_flow


class ClearableStore(MemoryStore):
    def clear_cache(self):
        self.cache.clear()

    def cache_info(self):
        return {"entries": len(self.cache), "bytes": len(json.dumps(self.cache))}


def test_latest_results_restore_offline_with_page_and_selection_but_not_reader_state():
    async def run():
        source, store = fixture_flow(), MemoryStore()
        first = engine(source, store)
        await first.search(SearchFilters("rain", language="en"))
        await first.open_result(0)
        await first.next_results()
        first.set_result_selection(1)
        first.search_draft = {"query": "unsubmitted snow"}
        first.save()
        restored = engine(source, store)
        calls = len(source.calls)
        page = restored.restore_results()
        assert [work.id for work in page.items] == ["B", "D"]
        assert restored.results_page_number == 2 and restored.results_selection == 1
        assert restored.filters.query == "rain" and restored.filters.language == "en"
        assert restored.current is None and restored.position == {}
        assert restored.search_draft == {}  # This API restores only committed results.
        assert len(source.calls) == calls

    asyncio.run(run())


def test_never_searched_and_actual_empty_page_remain_distinct_after_restart():
    async def run():
        source, store = FixtureSource(), MemoryStore()
        reader = engine(source, store)
        assert reader.restore_results() is None
        source.search_pages["empty", None] = Page([], "empty-page")
        await reader.search(SearchFilters("empty"))
        restored = engine(source, store)
        assert restored.latest_search_page() == Page([], "empty-page")
        assert restored.filters.query == "empty"

    asyncio.run(run())


def test_direct_open_does_not_overwrite_the_persisted_latest_search():
    async def run():
        source, store = fixture_flow(), MemoryStore()
        first = engine(source, store)
        await first.search(SearchFilters("rain"))
        first.search_draft = {"query": "unsubmitted draft"}
        first.save()
        second = engine(source, store)
        await second.open_url(source.works["E"].work.url)  # CLI --open bypasses start().
        assert second.current.id == "E1"
        assert [w.id for w in second.restore_results().items] == ["A", "C"]
        assert store.load()["results"]["url"] == "search1"
        assert store.load()["search_draft"]["query"] == "unsubmitted draft"

    asyncio.run(run())


def test_clear_prepared_cache_preserves_reader_and_search_and_blocks_late_refill():
    async def run():
        source, store = fixture_flow(), ClearableStore()
        reader = engine(source, store)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        reader.set_position({"paragraph": 1, "offset": 5})
        reader.search_draft = {"query": "private draft"}
        gate, original = asyncio.Event(), source.get_chapter
        target = reader.current.next_url

        async def delayed(url):
            if url == target:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
            return await original(url)

        source.get_chapter = delayed
        loader = asyncio.create_task(reader.prefetch())
        for _ in range(4):
            await asyncio.sleep(0)
        result = reader.clear_prepared_cache()
        assert result["cleared"] and result["paused"] and result["entries"] == 0
        assert reader.current.id == "A1" and reader.position == {"paragraph": 1, "offset": 5}
        assert reader.filters.query == "rain" and reader.search_draft["query"] == "private draft"
        assert reader.results.url == "search1"
        await asyncio.sleep(0)
        gate.set()
        await loader
        calls = len(source.calls)
        await reader.prefetch()
        assert len(source.calls) == calls and not store.cache
        status = reader.prefetch_status
        assert status["ready"] == status["previous_ready"] == 0
        assert status["work_branches"] == []
        assert store.load()["current"]["id"] == "A1"
        assert store.load()["search_draft"]["query"] == "private draft"

    asyncio.run(run())


def test_clear_prevents_late_foreground_body_from_refilling_sqlite():
    async def run():
        source, store = fixture_flow(), ClearableStore()
        reader = engine(source, store)
        await reader.open_url(source.works["A"].work.url)
        gate = asyncio.Event()
        source.gates["work", source.works["E"].work.url] = gate
        pending = asyncio.create_task(reader.open_url(source.works["E"].work.url))
        await asyncio.sleep(0)
        reader.clear_prepared_cache()
        gate.set()
        await pending
        assert reader.current.id == "A1" and not store.cache

    asyncio.run(run())


def test_long_result_browsing_keeps_bounded_pages_and_correct_absolute_page_number():
    async def run():
        source = FixtureSource()
        for number in range(1, 25):
            key = None if number == 1 else f"page{number}"
            source.search_pages["query", key] = Page(
                [source.add_work(f"W{number}"), source.add_work(f"X{number}")],
                f"page{number}",
                f"page{number + 1}" if number < 24 else None,
                f"page{number - 1}" if number > 1 else None,
            )
        reader = engine(source)
        await reader.search(SearchFilters("query"))
        for _ in range(23):
            reader.set_result_selection(1)
            await reader.next_results()
        assert len(reader._result_pages) <= 5 and reader.results_page_number == 24
        assert (await reader.prev_results()).url == "page23" and reader.results_selection == 1
        assert reader.results_page_number == 23
        saved = reader.resume_snapshot()
        assert saved["results"]["url"] == "page23" and saved["results_page_number"] == 23

    asyncio.run(run())


def test_real_previous_cursors_allow_ten_page_round_trip_with_five_cached_pages():
    async def run():
        source = FixtureSource()
        for number in range(1, 11):
            page = Page(
                [source.add_work(f"W{number}")],
                f"page{number}",
                f"page{number + 1}" if number < 10 else None,
                f"page{number - 1}" if number > 1 else None,
            )
            source.search_pages["query", f"page{number}"] = page
            if number == 1:
                source.search_pages["query", None] = page
        reader = engine(source)
        await reader.search(SearchFilters("query", language="en"))
        for number in range(2, 11):
            assert reader.has_next_results
            page = await reader.next_results()
            assert page.url == f"page{number}"
            assert reader.results_page_number == number
            assert len(reader._result_pages) <= 5
        assert not reader.has_next_results
        for number in range(9, 0, -1):
            assert reader.has_previous_results
            page = await reader.prev_results()
            assert page.url == f"page{number}"
            assert reader.results_page_number == number
            assert reader.filters == SearchFilters("query", language="en")
            assert len(reader._result_pages) <= 5
        assert not reader.has_previous_results
        assert ("search", ("query", "page5")) in source.calls
        assert ("search", ("query", "page1")) in source.calls

    asyncio.run(run())


def test_tenth_result_page_restores_offline_then_can_return_to_first_page():
    async def run():
        source, store = FixtureSource(), MemoryStore()
        for number in range(1, 11):
            page = Page(
                [source.add_work(f"W{number}"), source.add_work(f"X{number}")],
                f"page{number}",
                f"page{number + 1}" if number < 10 else None,
                f"page{number - 1}" if number > 1 else None,
            )
            source.search_pages["query", f"page{number}"] = page
            if number == 1:
                source.search_pages["query", None] = page
        original = engine(source, store)
        await original.search(SearchFilters("query", language="en"))
        for _ in range(9):
            await original.next_results()
        original.set_result_selection(1)
        assert original.save()
        requests = len(source.calls)
        restored = engine(source, store)
        assert restored.restore_results().url == "page10"
        assert len(source.calls) == requests  # /results restores without fetching.
        assert restored.results_page_number == 10 and restored.results_selection == 1
        assert len(restored._result_pages) == 1
        for number in range(9, 0, -1):
            assert restored.has_previous_results
            assert (await restored.prev_results()).url == f"page{number}"
            assert restored.results_page_number == number
            assert len(restored._result_pages) <= 5
        assert not restored.has_previous_results
        assert restored.filters == SearchFilters("query", language="en")
        assert store.load()["results_page_number"] == 1

    asyncio.run(run())


def test_snapshot_limits_accumulating_metadata_without_cutting_current_body():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        original = copy.deepcopy(reader.current.paragraphs)
        reader._seen = {f"W{i}:chapter" for i in range(10000)}
        reader._finished = {f"W{i}": {"count": 1} for i in range(10000)}
        reader._flow.skipped = [str(i) for i in range(10000)]
        reader._flow.origin.visited_urls = [f"https://reader.example/{i}" for i in range(10000)]
        saved = reader.resume_snapshot()
        assert saved["current"]["paragraphs"] == original
        assert len(saved["seen"]) <= 2048 and len(saved["finished"]) <= 512
        assert len(saved["flow"]["skipped"]) <= 1024
        assert len(saved["flow"]["origin"]["visited_urls"]) <= 128
        assert "flow" not in saved["history"][0]
        assert len(json.dumps(saved).encode()) < 512 * 1024

    asyncio.run(run())


def test_oversize_save_keeps_full_current_in_memory_and_old_valid_state_on_disk(tmp_path):
    from mini_notes.storage import SESSION_LIMIT_BYTES, StateStore

    async def run():
        source = fixture_flow()
        store = StateStore(tmp_path)
        reader = engine(source, store)
        await reader.open_url(source.works["A"].work.url)
        huge = "原" * (SESSION_LIMIT_BYTES // 3 + 1)
        source.works["E"].chapter.paragraphs = [huge]
        await reader.open_url(source.works["E"].work.url)
        assert reader.current.id == "E1" and reader.current.paragraphs == [huge]
        assert reader.storage_notice and "未保存" in reader.storage_notice
        assert store.load()["current"]["id"] == "A1"
        assert reader.save() is False
        reader.current.paragraphs = ["另一个较短的原创测试段落。"]
        assert reader.save() is True and reader.storage_notice == ""
        assert store.load()["current"]["id"] == "E1"
        await reader.close()
        store.close()

    asyncio.run(run())


def test_resume_uses_compact_history_and_continues_across_replaced_queue_pages():
    async def run():
        source, store = FixtureSource(), MemoryStore()
        works = [source.add_work(str(i)) for i in range(15)]
        for i, work in enumerate(works):
            source.search_pages["q", None if i == 0 else f"page{i}"] = Page(
                [work], f"page{i}", f"page{i + 1}" if i < 14 else None
            )
        reader = engine(source, store)
        await reader.search(SearchFilters("q"))
        await reader.open_result(0)
        for expected in range(1, 10):
            assert (await reader.next()).work.id == str(expected)
            assert len(reader._flow.origin.items) == 1
        reader.set_position({"paragraph": 1, "offset": 2})
        reader.save()
        saved = store.load()
        assert "flow" not in saved["history"][0]
        restored = engine(source, store)
        await restored.start()
        assert restored.current.work.id == "9"
        assert restored.position == {"paragraph": 1, "offset": 2}
        assert (await restored.next()).work.id == "10"

    asyncio.run(run())


def test_clear_compacts_disk_and_saves_latest_reader_state(tmp_path):
    from mini_notes.storage import StateStore

    async def run():
        source, store = fixture_flow(), StateStore(tmp_path)
        reader = engine(source, store)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        reader.set_position({"paragraph": 1, "offset": 4})
        reader.search_draft = {"query": "最新原创草稿"}
        assert store.cache_put("original-padding", {"text": "原" * 2_000_000})
        before = store.path.stat().st_size
        result = reader.clear_prepared_cache()
        assert result["cleared"] and result["saved"] and result["entries"] == 0
        assert store.path.stat().st_size < before // 2
        assert reader.current.id == "A1" and reader.position["offset"] == 4
        assert store.load()["position"] == reader.position
        assert store.load()["search_draft"] == reader.search_draft
        assert store.load()["results"]["url"] == "search1"
        await reader.close()
        store.close()

    asyncio.run(run())


def test_clear_compacts_even_if_current_is_unsavable_without_replacing_old_session(tmp_path):
    from mini_notes.storage import SESSION_LIMIT_BYTES, StateStore

    async def run():
        source, store = fixture_flow(), StateStore(tmp_path)
        reader = engine(source, store)
        await reader.open_url(source.works["A"].work.url)
        old_saved = store.load()
        reader.current.paragraphs = ["原" * (SESSION_LIMIT_BYTES // 3 + 1)]
        reader.set_position({"paragraph": 0, "offset": 19})
        reader.search_draft = {"query": "尚未保存的原创草稿"}
        assert store.cache_put("original-padding", {"text": "填" * 2_000_000})
        before = store.path.stat().st_size
        result = reader.clear_prepared_cache()
        assert result["cleared"] and not result["saved"] and result["entries"] == 0
        assert store.path.stat().st_size < before // 2
        assert store.load() == old_saved
        assert reader.position["offset"] == 19 and reader.search_draft["query"]
        assert len(reader.current.paragraphs[0]) > SESSION_LIMIT_BYTES // 3
        assert "未保存" in reader.storage_notice
        reader.notice = "普通准备状态"
        assert "未保存" in reader.storage_notice
        await reader.close()
        assert source.closed and store.cache_info()["entries"] == 0
        assert store.load() == old_saved
        store.close()

    asyncio.run(run())


def test_clear_reports_compaction_failure_without_claiming_success():
    from mini_notes.storage import StorageError

    class FailingCompactStore(ClearableStore):
        def compact_on_exit(self):
            raise StorageError("原创测试：磁盘空间不足")

    async def run():
        source, store = fixture_flow(), FailingCompactStore()
        reader = engine(source, store)
        await reader.open_url(source.works["A"].work.url)
        result = reader.clear_prepared_cache()
        assert result["cleared"] is False and result["saved"] is True
        assert "未能" in reader.storage_notice and "清理" in reader.storage_notice
        assert reader.current.id == "A1" and store.cache

    asyncio.run(run())


def test_oversized_legacy_restores_full_body_and_reports_unsaved_state(tmp_path):
    from mini_notes.storage import SESSION_LIMIT_BYTES, StateStore

    async def run():
        source, store = fixture_flow(), StateStore(tmp_path)
        first = engine(source)
        await first.open_url(source.works["A"].work.url)
        saved = first.resume_snapshot()
        saved["current"]["paragraphs"] = ["原" * (SESSION_LIMIT_BYTES // 3 + 1)]
        with store._db:
            store._db.execute("INSERT INTO state VALUES('session',?)", (
                json.dumps(saved, ensure_ascii=False, separators=(",", ":")),
            ))
        store.last_error = {"code": "storage_limit", "scope": "session"}
        calls = len(source.calls)
        reader = engine(source, store)
        await reader.start()
        assert reader.current.paragraphs == saved["current"]["paragraphs"]
        assert len(source.calls) == calls and "未保存" in reader.storage_notice
        assert store.load() == saved
        await reader.close()
        assert source.closed and store.cache_info()["entries"] == 0
        store.close()

    asyncio.run(run())


def test_initial_state_preserves_unsaved_language_override_and_original_disk(tmp_path):
    from mini_notes.storage import SESSION_LIMIT_BYTES, StateStore

    async def run():
        source, store = fixture_flow(), StateStore(tmp_path)
        first = engine(source, store)
        await first.search(SearchFilters("rain", language="en"))
        await first.open_result(0)
        old_saved = store.load()
        prepared = copy.deepcopy(old_saved)
        prepared["filters"]["language"] = "zh"
        prepared["flow"]["filters"]["language"] = "zh"
        prepared["current"]["paragraphs"] = ["原" * (SESSION_LIMIT_BYTES // 3 + 1)]
        prepared["search_draft"] = {"query": "未落盘草稿", "language": "zh"}
        reader = ReaderEngine(source, store, initial_state=prepared)
        prepared["filters"]["language"] = "fr"  # Caller mutation must not leak.
        await reader.start()
        assert reader.filters.language == reader._flow.filters.language == "zh"
        assert reader.search_draft["language"] == "zh"
        assert "未保存" in reader.storage_notice and store.load() == old_saved
        await reader.close()
        store.close()

    asyncio.run(run())


def test_initial_state_is_used_by_direct_open_even_without_search_results():
    async def run():
        source, store = fixture_flow(), MemoryStore()
        first = engine(source, store)
        await first.open_url(source.works["A"].work.url)
        prepared = first.resume_snapshot()
        prepared["filters"]["language"] = "en"
        prepared["search_draft"] = {"query": "内存草稿"}
        reader = ReaderEngine(source, store, initial_state=prepared)
        await reader.open_url(source.works["E"].work.url)
        assert reader.filters.language == "en" and reader.search_draft["query"] == "内存草稿"
        assert reader.current.id == "E1"
        assert store.load()["filters"]["language"] == "en"

    asyncio.run(run())
