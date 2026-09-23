"""Original branch fixtures verify unread predecessors and bounded skip coverage."""

import asyncio
import json
import time

import pytest

from mini_notes.models import Page, SearchFilters, SeriesDetail, SeriesRef
from mini_notes.source import SourceError
from test_engine import FixtureSource, MemoryStore, engine, error, fixture_flow


def branching_source():
    source = FixtureSource()
    series = SeriesRef("S", "https://reader.example/series/S", "原创有序系列")
    works = [source.add_work(key, 4, [series]) for key in "ABCD"]
    extras = [source.add_work(key, 4) for key in "EFG"]
    source.series[series.url] = SeriesDetail("S", series.url, series.title, works)
    source.search_pages["rain", None] = Page([works[1], *extras[:2]], "search1")
    source.recent_pages[None] = Page([extras[2]], "recent1")
    return source


def test_unread_previous_chapter_preserves_origin_anchor_when_going_forward():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.open_url(source.works["B"].chapters[2].url)
        reader.set_position({"paragraph": 1, "offset": 7})
        assert (await reader.previous()).id == "B2"
        assert (await reader.previous()).id == "B1"
        assert (await reader.next()).id == "B2"
        assert (await reader.next()).id == "B3"
        assert reader.position == {"paragraph": 1, "offset": 7}

    asyncio.run(run())


def test_backward_entry_prefetches_earlier_context_and_forward_history_does_not_repeat():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.open_url(source.works["B"].chapters[2].url)
        reader.set_position({"paragraph": 1, "offset": 7})
        assert (await reader.previous()).id == "B2"
        await reader.prefetch()
        assert reader.prefetch_status["previous_ready"] == 1
        calls = len(source.calls)
        assert (await reader.next()).id == "B3"
        assert reader.position == {"paragraph": 1, "offset": 7}
        assert (await reader.next()).id == "B4"
        assert len(source.calls) == calls

    asyncio.run(run())


def test_first_chapter_can_open_unread_previous_series_work_last_chapter_after_restart():
    async def run():
        source, store = branching_source(), MemoryStore()
        reader = engine(source, store)
        await reader.open_url(source.works["B"].work.url)
        reader.set_position({"paragraph": 1, "offset": 3})
        reader.save()
        restored = engine(source, store)
        await restored.start()
        assert (await restored.previous()).id == "A4"
        assert restored.selected_series.endswith("/S")
        assert (await restored.next()).id == "B1"
        assert restored.position == {"paragraph": 1, "offset": 3}

    asyncio.run(run())


def test_restart_from_previous_series_work_returns_to_its_canonical_successor_first_chapter():
    async def run():
        source, store = branching_source(), MemoryStore()
        reader = engine(source, store)
        await reader.open_url(source.works["B"].work.url)
        assert (await reader.previous()).id == "A4"
        reader.save()
        restored = engine(source, store)
        await restored.start()
        assert (await restored.next()).id == "B1"

    asyncio.run(run())


def test_branch_window_primes_skip_and_two_more_chapters_without_network():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.search(SearchFilters("rain", language="en"))
        await reader.open_result(0)
        await reader.next()
        await reader.prefetch()
        status = reader.prefetch_status
        assert status["previous_ready"] == 1
        assert status["skip_work_ready"] and 'skip_series_ready' not in status
        assert 'skip_series' not in reader._branch_done
        assert {b["work_id"] for b in status["work_branches"]} == {"C", "D", "E"}
        assert all(b["ready"] == 3 for b in status["work_branches"])
        calls = len(source.calls)
        assert (await reader.skip_work()).id == "C1"
        assert (await reader.next()).id == "C2"
        assert (await reader.next()).id == "C3"
        assert len(source.calls) == calls

    asyncio.run(run())


def test_predecessor_respects_hard_filters_and_does_not_treat_collection_as_series():
    async def run():
        source = branching_source()
        source.works["A"].work.categories = ["M/M"]
        source.series[next(iter(source.series))].works[0].categories = ["M/M"]
        reader = engine(source)
        reader.filters = SearchFilters(categories=["21"])
        await reader.open_url(source.works["B"].work.url)
        assert await reader.previous() is None
        assert reader.current.id == "B1"
        assert ("work", source.works["A"].work.url) not in source.calls
        await reader.open_url(source.works["E"].work.url)
        calls = len(source.calls)
        # A work without a SeriesRef does not infer an ordering from arbitrary
        # collection/group metadata. It can only return its actual reading history.
        assert (await reader.previous()).id == "B1"
        assert len(source.calls) == calls

    asyncio.run(run())


def test_failed_predecessor_preserves_current_body_and_anchor():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.open_url(source.works["B"].chapters[2].url)
        reader.set_position({"paragraph": 1, "offset": 8})
        source.fail["chapter", reader.current.previous_url] = error("network")
        with pytest.raises(Exception, match="network"):
            await reader.previous()
        assert reader.current.id == "B3"
        assert reader.position == {"paragraph": 1, "offset": 8}

    asyncio.run(run())


def test_predecessor_cycle_stops_without_replacing_the_reader():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.open_url(source.works["B"].chapters[2].url)
        bad_previous = source.chapters[source.works["B"].chapters[1].url]
        bad_previous.previous_url = source.works["B"].chapters[2].url
        assert (await reader.previous()).id == "B2"
        with pytest.raises(SourceError) as exc:
            await reader.previous()
        assert exc.value.code == "navigation_cycle" and reader.current.id == "B2"

    asyncio.run(run())


def test_pending_skip_reuses_same_branch_request_and_old_epoch_does_not_publish():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        gate = asyncio.Event()
        source.gates["work", source.works["C"].work.url] = gate
        pending = asyncio.create_task(reader.prefetch())
        for _ in range(12):
            await asyncio.sleep(0)
        skip = asyncio.create_task(reader.skip_work())
        for _ in range(4):
            await asyncio.sleep(0)
        assert not skip.done()
        gate.set()
        assert (await skip).id == "C1"
        await pending
        assert source.calls.count(("work", source.works["C"].work.url)) == 1
        await reader.search(SearchFilters("rain"))
        assert reader.prefetch_status["work_branches"] == []

    asyncio.run(run())


def test_background_transient_failure_recovers_within_one_shared_worker():
    async def run():
        source = branching_source()
        reader = engine(source)
        reader.PREFETCH_RETRY_DELAYS = (0.001, 0.001)
        await reader.open_url(source.works["B"].work.url)
        target = reader.current.next_url
        get_chapter, attempts = source.get_chapter, []

        async def transient(url):
            if url == target:
                attempts.append(url)
                if len(attempts) < 3:
                    exc = error("network")
                    exc.retryable = True
                    raise exc
            return await get_chapter(url)

        source.get_chapter = transient
        await reader.prefetch()
        assert len(attempts) == 3
        assert reader.prefetch_status["ready"] == 3
        assert reader.current.id == "B1"
        assert (await reader.next()).id == "B2" and len(attempts) == 3

    asyncio.run(run())


def test_manual_prefetch_retry_preserves_ready_and_errors_do_not_multiply_automatically():
    async def run():
        source = branching_source()
        reader = engine(source)
        reader.PREFETCH_RETRY_DELAYS = (0.001, 0.001)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        target = source.works["C"].work.url
        failure = error("network")
        failure.retryable = True
        source.fail["work", target] = failure
        await reader.prefetch()
        assert source.calls.count(("work", target)) == 3
        assert reader.prefetch_status["ready"] == 3
        await reader.prefetch()
        assert source.calls.count(("work", target)) == 3
        with pytest.raises(SourceError) as exc:
            await reader.skip_work()
        assert exc.value.recovery_exhausted and exc.value.attempts == 3
        assert reader.current.id == "B1" and reader.prefetch_status["ready"] == 3
        del source.fail["work", target]
        await reader.retry_prefetch()
        assert reader.prefetch_status["skip_work_ready"]
        assert reader.prefetch_status["ready"] == 3 and reader.current.id == "B1"
        assert source.calls.count(("work", target)) == 4

    asyncio.run(run())


def test_long_retry_after_pauses_other_branches_and_manual_retry_respects_countdown():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.open_url(source.works["B"].work.url)
        failure = SourceError("rate_limited", "自拟限流", retry_after=120)
        failure.retryable = True
        source.fail["chapter", reader.current.next_url] = failure
        calls = len(source.calls)
        await reader.prefetch()
        status = reader.prefetch_status
        assert status["cooldown"] and status["retry_in"] > 100
        assert len(source.calls) - calls == 1
        await reader.retry_prefetch()
        assert len(source.calls) - calls == 1
        assert reader.current.id == "B1"

    asyncio.run(run())


def test_whole_prefetch_cycle_has_one_deadline_not_one_per_branch():
    async def run():
        source = branching_source()
        reader = engine(source)
        reader.PREFETCH_SECONDS = 0.025
        await reader.open_url(source.works["B"].work.url)
        source.gates["chapter", reader.current.next_url] = asyncio.Event()
        calls, started = len(source.calls), time.monotonic()
        await reader.prefetch()
        assert time.monotonic() - started < 0.3
        assert len(source.calls) - calls == 1
        assert reader.current.id == "B1"
        assert reader.prefetch_status["errors"]

    asyncio.run(run())


def test_cancel_resistant_old_branch_cannot_change_new_search_or_preparation():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        gate = asyncio.Event()
        original = source.get_work
        target = source.works["C"].work.url

        async def delayed(url):
            if url == target:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
            return await original(url)

        source.get_work = delayed
        pending = asyncio.create_task(reader.prefetch())
        for _ in range(12):
            await asyncio.sleep(0)
        source.search_pages["new", None] = Page([source.works["E"].work], "new-search")
        await reader.search(SearchFilters("new"))
        await reader.open_result(0)
        gate.set()
        await pending
        assert reader.current.id == "E1" and reader.filters.query == "new"
        assert reader.prefetch_status["work_branches"] == []
        assert reader.prefetch_status["previous_ready"] == 0
        assert reader.prefetch_status["ready"] == 0

    asyncio.run(run())


def test_branch_budget_is_shared_and_resume_never_contains_branch_bodies():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        calls = len(source.calls)
        await reader.prefetch()
        assert len(source.calls) - calls <= 24
        saved = json.dumps(reader.resume_snapshot(), ensure_ascii=False)
        assert "自拟段落 B1-001" in saved
        for ident in ("A4", "B2", "B3", "C1", "C2", "D1", "E1", "E3"):
            assert f"自拟段落 {ident}-001" not in saved

    asyncio.run(run())


def test_cancel_promoted_next_pauses_background_retry_and_keeps_current_anchor():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.open_url(source.works["B"].work.url)
        reader.set_position({"paragraph": 1, "offset": 5})
        gate, promoted = asyncio.Event(), []
        source.gates["chapter", reader.current.next_url] = gate
        source.promote = lambda task: promoted.append(task)
        loader = asyncio.create_task(reader.prefetch())
        for _ in range(4):
            await asyncio.sleep(0)
        advancing = asyncio.create_task(reader.next())
        for _ in range(4):
            await asyncio.sleep(0)
        assert promoted and not advancing.done()
        advancing.cancel()
        reader.pause_prefetch()
        await asyncio.gather(loader, advancing, return_exceptions=True)
        calls = len(source.calls)
        await reader.prefetch()
        assert len(source.calls) == calls and reader.prefetch_status["paused"]
        assert reader.current.id == "B1"
        assert reader.position == {"paragraph": 1, "offset": 5}
        gate.set()
        await reader.retry_prefetch()
        assert not reader.prefetch_status["paused"]
        assert reader.prefetch_status["ready"] == 3 and reader.current.id == "B1"

    asyncio.run(run())


def test_pause_preserves_ready_and_rejects_late_result_without_changing_reading_epoch():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        gate = asyncio.Event()
        original = source.get_chapter
        target = source.works["C"].chapters[1].url

        async def delayed(url):
            if url == target:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
            return await original(url)

        source.get_chapter = delayed
        loader = asyncio.create_task(reader.prefetch())
        for _ in range(20):
            await asyncio.sleep(0)
        assert reader.prefetch_status["ready"] == 3
        before = reader.prefetch_status["work_branches"]
        epoch = reader._epoch
        reader.pause_prefetch()
        await asyncio.sleep(0)
        gate.set()
        await loader
        assert reader._epoch == epoch and reader.current.id == "B1"
        assert reader.prefetch_status["work_branches"] == before
        assert reader.prefetch_status["ready"] == 3
        assert reader.store.cache_get("chapter:" + target) is None
        assert (await reader.next()).id == "B2"

    asyncio.run(run())


def test_consuming_forward_error_keeps_other_branch_worker_owned_until_close():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        source.fail["chapter", reader.current.next_url] = error("parse_error")
        source.gates["work", source.works["B"].work.url] = asyncio.Event()
        loader = asyncio.create_task(reader.prefetch())
        for _ in range(12):
            await asyncio.sleep(0)
        worker = reader._prefetch_task
        assert worker and not worker.done()
        with pytest.raises(SourceError):
            await reader.next()
        assert reader._prefetch_task is worker and not worker.done()
        await reader.close()
        await loader
        assert worker.done() and source.closed

    asyncio.run(run())


def test_cached_canonical_previous_never_waits_on_next_chapter_network():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.open_url(source.works["B"].work.url)
        reader.set_position({"paragraph": 1, "offset": 6})
        await reader.next()
        source.gates["chapter", reader.current.next_url] = asyncio.Event()
        loader = asyncio.create_task(reader.prefetch())
        for _ in range(4):
            await asyncio.sleep(0)
        calls = len(source.calls)
        previous = await asyncio.wait_for(reader.previous(), timeout=0.1)
        assert previous.id == "B1"
        assert reader.position == {"paragraph": 1, "offset": 6}
        assert len(source.calls) == calls
        await loader

    asyncio.run(run())


def test_explicit_next_after_pause_resumes_prefetch_but_polling_does_not():
    async def run():
        source = branching_source()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        gate, intercepted = asyncio.Event(), False
        original = source.get_chapter
        target = source.works["C"].chapters[1].url

        async def delayed_once(url):
            nonlocal intercepted
            if url == target and not intercepted:
                intercepted = True
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
            return await original(url)

        source.get_chapter = delayed_once
        old_loader = asyncio.create_task(reader.prefetch())
        for _ in range(20):
            await asyncio.sleep(0)
        assert reader.prefetch_status["ready"] == 3
        reader.pause_prefetch()
        await asyncio.sleep(0)
        calls = len(source.calls)
        await reader.prefetch()
        assert reader.prefetch_status["paused"] and len(source.calls) == calls
        assert (await reader.next()).id == "B2"
        assert not reader.prefetch_status["paused"]
        await reader.prefetch()
        assert reader.prefetch_status["ready"] == 3
        assert len(source.calls) > calls
        current, prepared = reader.current.id, reader.prefetch_status["work_branches"]
        gate.set()
        await old_loader
        assert reader.current.id == current == "B2"
        assert reader.prefetch_status["work_branches"] == prepared
        assert reader.prefetch_status["ready"] == 3

    asyncio.run(run())
