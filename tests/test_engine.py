"""State-machine tests use only self-authored in-memory source fixtures."""

import asyncio
import copy
import importlib

import pytest

from mini_notes.models import (
    Chapter,
    ChapterRef,
    Page,
    SearchFilters,
    SeriesDetail,
    SeriesRef,
    WorkDetail,
    WorkSummary,
)


class MemoryStore:
    def __init__(self):
        self.state = {}
        self.cache = {}

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, value):
        self.state = copy.deepcopy(value)

    def cache_get(self, key):
        return copy.deepcopy(self.cache.get(key))

    def cache_put(self, key, value, ttl=86400):
        self.cache[key] = copy.deepcopy(value)

    def close(self):
        pass


def work(key, **overrides):
    values = dict(
        id=key,
        url=f"https://reader.example/works/{key}",
        title=f"原创作品 {key}",
        authors=["自拟作者"],
        warnings=["No Archive Warnings Apply"],
        categories=["Gen"],
        rating="General Audiences",
        language="English",
        language_id="en",
        chapter_count=1,
        chapter_total=1,
    )
    values.update(overrides)
    return WorkSummary(**values)


class FixtureSource:
    """Only the external I/O boundary is replaced; real engine and models run."""

    def __init__(self):
        self.calls = []
        self.fail = {}
        self.gates = {}
        self.works = {}
        self.chapters = {}
        self.series = {}
        self.search_pages = {}
        self.recent_pages = {}
        self.closed = False

    def add_work(self, key, count=1, series=(), **metadata):
        w = work(key, chapter_count=count, chapter_total=count, **metadata)
        refs = [
            ChapterRef(f"{key}{i}", f"{w.url}/chapters/{key}{i}", f"第{i}章", i)
            for i in range(1, count + 1)
        ]
        for i, ref in enumerate(refs):
            self.chapters[ref.url] = Chapter(
                ref.id,
                ref.url,
                ref.title,
                [f"自拟段落 {ref.id}-001", f"自拟段落 {ref.id}-002"],
                w,
                refs[i - 1].url if i else None,
                refs[i + 1].url if i + 1 < count else None,
                list(series),
                i + 1,
            )
        detail = WorkDetail(w, self.chapters[refs[0].url], refs, list(series))
        self.works[w.url] = detail
        self.works[key] = detail
        return w

    async def _before(self, method, key):
        self.calls.append((method, key))
        if (method, key) in self.gates:
            await self.gates[method, key].wait()
        if (method, key) in self.fail:
            raise self.fail[method, key]

    async def search(
        self, query="", warnings=(), categories=(), page_url=None, rating="", language=""
    ):
        await self._before("search", (query, page_url))
        return copy.deepcopy(self.search_pages[query, page_url])

    async def recent(self, page_url=None, warnings=(), categories=(), rating="", language=""):
        await self._before("recent", page_url)
        self.recent_filters = (list(warnings), list(categories), rating, language)
        return copy.deepcopy(self.recent_pages[page_url])

    async def get_work(self, ref):
        await self._before("work", ref)
        return copy.deepcopy(self.works[ref])

    async def get_chapter(self, ref):
        await self._before("chapter", ref)
        return copy.deepcopy(self.chapters[ref])

    async def get_series(self, ref):
        await self._before("series", ref)
        return copy.deepcopy(self.series[ref])

    async def aclose(self):
        self.closed = True


def engine(source, store=None):
    return importlib.import_module("mini_notes.engine").ReaderEngine(source, store or MemoryStore())


def error(code):
    exc = importlib.import_module("mini_notes.source").SourceError(code, f"自拟错误 {code}")
    exc.retryable = False  # Recovery timing is exercised by dedicated branch tests.
    return exc


def fixture_flow():
    source = FixtureSource()
    series = SeriesRef("S", "https://reader.example/series/S", "自拟系列")
    a = source.add_work("A", 2, [series])
    b = source.add_work("B", series=[series])
    c = source.add_work("C")
    d = source.add_work("D", categories=["M/M"])
    e = source.add_work("E")
    source.series[series.url] = SeriesDetail("S", series.url, series.title, [a, b])
    source.search_pages["rain", None] = Page([a, c], "search1", "search2")
    source.search_pages["rain", "search2"] = Page([b, c, d], "search2")
    source.recent_pages[None] = Page([b, e], "recent1")
    return source


def test_same_work_then_series_then_original_pagination_then_recent_preserves_hard_filters():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain", ["16"], ["21"], "10", "en"))
        seen = [(await reader.open_result(0)).id]
        for _ in range(4):
            seen.append((await reader.next()).id)
        assert seen == ["A1", "A2", "B1", "C1", "E1"]
        assert reader.filters.query == "rain"
        assert reader.source_label == "最近更新"
        assert "搜索结果已浏览完毕" in reader.notice
        assert [x.id for x in reader.results.items][:2] == ["A", "C"]
        assert source.recent_filters == (["16"], ["21"], "10", "en")
        assert ("work", "https://reader.example/works/D") not in source.calls

    asyncio.run(run())


def test_timeout_and_failed_prefetch_do_not_replace_current_or_consume_successor():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        reader.set_position({"paragraph": 1, "offset": 4})
        successor = reader.current.next_url
        source.fail["chapter", successor] = error("timeout")
        await reader.prefetch()
        assert reader.current.id == "A1" and reader.position["offset"] == 4
        with pytest.raises(Exception) as raised:
            await reader.next()
        assert raised.value.code == "timeout"
        assert reader.current.id == "A1" and reader.position["offset"] == 4
        assert not any(m == "recent" for m, _ in source.calls)
        del source.fail["chapter", successor]
        assert (await reader.next()).id == "A2"

    asyncio.run(run())


def test_prefetch_one_successor_is_reused_and_does_not_advance_history():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        await reader.prefetch()
        await reader.prefetch()
        assert reader.current.id == "A1"
        assert await reader.previous() is None
        assert (await reader.next()).id == "A2"
        assert source.calls.count(("chapter", "https://reader.example/works/A/chapters/A2")) == 1

    asyncio.run(run())


def test_restart_reads_saved_content_without_network_and_restores_search_flow():
    async def run():
        source = fixture_flow()
        store = MemoryStore()
        reader = engine(source, store)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        await reader.next()
        reader.set_position({"paragraph": 1, "offset": 3})
        reader.save()
        restarted_source = fixture_flow()
        restored = engine(restarted_source, store)
        assert (await restored.start()).id == "A2"
        assert restored.position == {"paragraph": 1, "offset": 3}
        assert restarted_source.calls == []
        assert (await restored.next()).id == "B1"
        assert (await restored.next()).id == "C1"
        assert restored.filters.query == "rain"

    asyncio.run(run())


def test_previous_and_forward_replay_restore_positions_and_queue():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        reader.set_position({"paragraph": 1, "offset": 7})
        await reader.next()
        reader.set_position({"paragraph": 0, "offset": 2})
        assert (await reader.previous()).id == "A1"
        assert reader.position["offset"] == 7
        before = len(source.calls)
        assert (await reader.next()).id == "A2"
        assert reader.position["offset"] == 2 and len(source.calls) == before
        assert (await reader.next()).id == "B1"

    asyncio.run(run())


def test_no_successor_keeps_current_and_does_not_loop_old_content():
    async def run():
        source = FixtureSource()
        a = source.add_work("A")
        source.recent_pages[None] = Page([a], "recent1")
        reader = engine(source)
        await reader.start()
        reader.set_position({"paragraph": 1})
        assert await reader.next() is None
        assert reader.current.id == "A1" and reader.position == {"paragraph": 1}
        assert reader.notice and len(source.calls) < 5

    asyncio.run(run())


def test_latest_search_wins_when_old_response_finishes_late():
    async def run():
        source = FixtureSource()
        a = source.add_work("A")
        b = source.add_work("B")
        source.search_pages["old", None] = Page([a], "old")
        source.search_pages["new", None] = Page([b], "new")
        gate = asyncio.Event()
        source.gates["search", ("old", None)] = gate
        reader = engine(source)
        old = asyncio.create_task(reader.search(SearchFilters("old")))
        await asyncio.sleep(0)
        await reader.search(SearchFilters("new"))
        gate.set()
        old_return = await old
        assert reader.filters.query == "new"
        assert [x.id for x in reader.results.items] == ["B"]
        assert [x.id for x in old_return.items] == ["B"]

    asyncio.run(run())


def test_new_open_invalidates_inflight_next_and_cancel_does_not_consume_cursor():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        gate = asyncio.Event()
        source.gates["chapter", reader.current.next_url] = gate
        pending = asyncio.create_task(reader.next())
        await asyncio.sleep(0)
        await reader.open_result(1)
        gate.set()
        await pending
        assert reader.current.id == "C1"
        await reader.open_result(0)
        # A distinct uncached successor lets cancellation exercise the actual I/O boundary.
        gate.clear()
        reader.store.cache.clear()
        pending = asyncio.create_task(reader.next())
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert reader.current.id == "A1"
        gate.set()
        assert (await reader.next()).id == "A2"

    asyncio.run(run())


def test_series_selection_overrides_default_without_dropping_search_origin():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        t = SeriesRef("T", "https://reader.example/series/T", "第二个自拟系列")
        detail = source.works["A"]
        detail.series.append(t)
        for ch in source.chapters.values():
            if ch.work.id == "A":
                ch.series.append(t)
        x = source.add_work("X", series=[t])
        source.series[t.url] = SeriesDetail("T", t.url, t.title, [detail.work, x])
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        assert reader.selected_series.endswith("/S")
        await reader.select_series(t.url)
        await reader.next()
        assert (await reader.next()).id == "X1"
        assert (await reader.next()).id == "C1"

    asyncio.run(run())


def test_adult_gate_is_not_confirmed_and_explicit_skip_can_continue():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        source.fail["chapter", reader.current.next_url] = error("adult_confirmation")
        with pytest.raises(Exception) as raised:
            await reader.next()
        assert raised.value.code == "adult_confirmation" and reader.current.id == "A1"
        assert (await reader.skip()).id == "B1"
        assert not any("view_adult" in str(key) for _, key in source.calls)

    asyncio.run(run())


def test_result_pagination_changes_page_and_timeout_is_retryable():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        source.fail["search", ("rain", "search2")] = error("timeout")
        with pytest.raises(Exception):
            await reader.more_results()
        assert [x.id for x in reader.results.items] == ["A", "C"]
        assert reader.results.next_url == "search2"
        del source.fail["search", ("rain", "search2")]
        assert [x.id for x in (await reader.more_results()).items] == ["B", "D"]

    asyncio.run(run())


def test_duplicate_only_pages_have_a_request_budget_and_do_not_claim_exhaustion():
    async def run():
        source = FixtureSource()
        a = source.add_work("A")
        for i in range(20):
            source.recent_pages[None if i == 0 else f"p{i}"] = Page([a], f"p{i}", f"p{i + 1}")
        reader = engine(source)
        await reader.start()
        assert await reader.next() is None
        assert reader.current.id == "A1"
        assert sum(method == "recent" for method, _ in source.calls) <= 5
        assert "本轮" in reader.notice

    asyncio.run(run())


def test_series_pagination_continues_in_source_order_before_returning_to_search():
    async def run():
        source = fixture_flow()
        s = "https://reader.example/series/S"
        source.series[s].works = [source.works["A"].work]
        source.series[s].next_url = s + "?page=2"
        source.series[s + "?page=2"] = SeriesDetail(
            "S", s + "?page=2", "自拟系列", [source.works["B"].work]
        )
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        await reader.next()
        assert (await reader.next()).id == "B1"
        assert (await reader.next()).id == "C1"

    asyncio.run(run())


def test_concurrent_next_does_not_jump_twice():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        gate = asyncio.Event()
        source.gates["chapter", reader.current.next_url] = gate
        first = asyncio.create_task(reader.next())
        await asyncio.sleep(0)
        assert await reader.next() is None
        gate.set()
        assert (await first).id == "A2"
        assert reader.current.id == "A2"

    asyncio.run(run())


def test_empty_body_is_error_not_successful_navigation():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        source.chapters[reader.current.next_url].paragraphs = ["  "]
        with pytest.raises(Exception) as raised:
            await reader.next()
        assert raised.value.code == "parse_error"
        assert reader.current.id == "A1"

    asyncio.run(run())


def test_stale_navigation_error_does_not_surface_after_opening_a_new_work():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        url = reader.current.next_url
        gate = asyncio.Event()
        source.gates["chapter", url] = gate
        source.fail["chapter", url] = error("timeout")
        old = asyncio.create_task(reader.next())
        await asyncio.sleep(0)
        await reader.open_result(1)
        gate.set()
        assert await old is None
        assert reader.current.id == "C1"

    asyncio.run(run())


def test_stale_search_error_does_not_replace_a_successful_new_search():
    async def run():
        source = fixture_flow()
        source.search_pages["new", None] = Page([source.works["C"].work], "new")
        gate = asyncio.Event()
        source.gates["search", ("old", None)] = gate
        source.fail["search", ("old", None)] = error("timeout")
        reader = engine(source)
        old = asyncio.create_task(reader.search(SearchFilters("old")))
        await asyncio.sleep(0)
        await reader.search(SearchFilters("new"))
        gate.set()
        assert [w.id for w in (await old).items] == ["C"]
        assert reader.filters.query == "new"

    asyncio.run(run())


def test_new_context_can_advance_while_old_navigation_is_still_pending():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        gate = asyncio.Event()
        source.gates["chapter", reader.current.next_url] = gate
        old = asyncio.create_task(reader.next())
        await asyncio.sleep(0)
        await reader.open_result(1)
        assert (await reader.next()).id == "B1"
        gate.set()
        assert await old is None
        assert reader.current.id == "B1"

    asyncio.run(run())


def test_failed_successor_exposes_its_safe_target_only_on_foreground_error():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        target = reader.current.next_url
        failure = error("adult_confirmation")
        failure.url = target
        source.fail["chapter", target] = failure
        await reader.prefetch()
        assert reader.pending_url is None
        with pytest.raises(Exception):
            await reader.next()
        assert reader.pending_url == target
        assert reader.current.url != target
        del source.fail["chapter", target]
        await reader.next()
        assert reader.pending_url is None

    asyncio.run(run())


def test_chapter_cycle_is_rejected_but_explicit_reopen_can_reread():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        source.chapters[
            "https://reader.example/works/A/chapters/A2"
        ].next_url = "https://reader.example/works/A/chapters/A1"
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        await reader.next()
        with pytest.raises(Exception) as raised:
            await reader.next()
        assert raised.value.code == "navigation_cycle"
        assert reader.current.id == "A2"
        await reader.open_result(0)
        assert (await reader.next()).id == "A2"

    asyncio.run(run())


def test_old_result_error_cannot_override_new_open():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        target = source.works["A"].work.url
        gate = asyncio.Event()
        source.gates["work", target] = gate
        source.fail["work", target] = error("timeout")
        old = asyncio.create_task(reader.open_result(0))
        await asyncio.sleep(0)
        await reader.open_result(1)
        gate.set()
        assert await old is None
        assert reader.current.id == "C1"
        assert reader.pending_url is None

    asyncio.run(run())


def test_automatic_candidates_skip_explicit_ratings_before_fetch_but_manual_open_uses_source():
    async def run():
        source = FixtureSource()
        mature = source.add_work("M", rating="Mature")
        explicit = source.add_work("E", rating="Explicit")
        general = source.add_work("G")
        source.recent_pages[None] = Page([mature, explicit, general], "recent1")
        source.search_pages["chosen", None] = Page([mature, explicit], "search1")
        reader = engine(source)
        assert (await reader.start()).work.id == "G"
        assert ("work", mature.url) not in source.calls
        assert ("work", explicit.url) not in source.calls
        await reader.search(SearchFilters("chosen"))
        source.fail["work", explicit.url] = error("adult_confirmation")
        with pytest.raises(Exception) as raised:
            await reader.open_result(1)
        assert raised.value.code == "adult_confirmation"
        assert ("work", explicit.url) in source.calls
        assert reader.current.work.id == "G"

    asyncio.run(run())


def test_real_result_pages_restore_selection_without_refetching_previous_page():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        reader.set_result_selection(1)
        assert reader.results_page_number == 1 and not reader.has_previous_results
        second = await reader.next_results()
        assert [w.id for w in second.items] == ["B", "D"]
        assert reader.results_page_number == 2 and reader.results_selection == 0
        reader.set_result_selection(1)
        calls = len(source.calls)
        first = await reader.prev_results()
        assert [w.id for w in first.items] == ["A", "C"]
        assert reader.results_selection == 1 and reader.results_page_number == 1
        await reader.next_results()
        assert reader.results_selection == 1 and len(source.calls) == calls

    asyncio.run(run())


def test_skip_work_retains_series_and_natural_continuation_without_series_skip():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain", categories=["21"]))
        await reader.open_result(0)
        assert (await reader.skip_work()).id == "B1"
        assert ("chapter", "https://reader.example/works/A/chapters/A2") not in source.calls
        assert not hasattr(reader, 'skip_series')
        assert (await reader.next()).id == "C1"
        assert (await reader.next()).id == "E1"
        assert reader.filters.query == "rain"

    asyncio.run(run())


def test_three_ready_successors_are_transactional_and_next_uses_no_source_calls():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain", categories=["21"]))
        await reader.open_result(0)
        await reader.prefetch()
        assert reader.current.id == "A1"
        assert reader.prefetch_status["ready"] == 3
        assert reader.prefetch_status["state"] == "ready"
        calls = len(source.calls)
        assert [(await reader.next()).id for _ in range(3)] == ["A2", "B1", "C1"]
        assert len(source.calls) == calls
        assert reader.prefetch_status["ready"] == 0

    asyncio.run(run())


def test_next_waits_for_inflight_prefetch_without_cancelling_or_restarting_it():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        target = reader.current.next_url
        gate = asyncio.Event()
        source.gates["chapter", target] = gate
        loader = asyncio.create_task(reader.prefetch())
        for _ in range(4):
            await asyncio.sleep(0)
        assert reader.prefetch_status["state"] == "loading"
        pending = asyncio.create_task(reader.next())
        for _ in range(4):
            await asyncio.sleep(0)
        assert not pending.done()
        assert source.calls.count(("chapter", target)) == 1
        gate.set()
        assert (await pending).id == "A2"
        await loader
        assert source.calls.count(("chapter", target)) == 1

    asyncio.run(run())


def test_new_search_invalidates_ready_and_pending_lookahead():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        await reader.prefetch()
        assert reader.prefetch_status["ready"] == 3
        source.search_pages["new", None] = Page([source.works["E"].work], "new")
        await reader.search(SearchFilters("new"))
        assert reader.prefetch_status["ready"] == 0
        await reader.open_result(0)
        assert reader.current.id == "E1"
        assert reader.prefetch_status["ready"] == 0

    asyncio.run(run())


def test_prefetch_error_is_visible_in_status_without_replacing_reader():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        target = reader.current.next_url
        source.fail["chapter", target] = error("timeout")
        await reader.prefetch()
        assert reader.prefetch_status["state"] == "error"
        assert reader.prefetch_status["ready"] == 0
        assert reader.current.id == "A1" and reader.pending_url is None

    asyncio.run(run())


def test_resume_snapshot_keeps_only_current_body_and_needed_queue_metadata():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        await reader.prefetch()
        await reader.next()
        reader.set_position({"paragraph": 1, "offset": 4})
        snapshot = reader.resume_snapshot()
        serialized = __import__("json").dumps(snapshot, ensure_ascii=False)
        assert "自拟段落 A2-001" in serialized
        assert "自拟段落 A1-001" not in serialized
        assert "自拟段落 B1-001" not in serialized
        assert "自拟段落 C1-001" not in serialized
        assert len(snapshot["history"]) == 1
        assert snapshot["position"] == {"paragraph": 1, "offset": 4}
        assert snapshot["filters"]["query"] == "rain"

    asyncio.run(run())


def test_cancel_resistant_old_prefetch_cannot_publish_after_a_new_search():
    async def run():
        source = fixture_flow()
        reader = engine(source)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        gate = asyncio.Event()
        original_get = source.get_chapter
        target = reader.current.next_url

        async def delayed(url):
            if url == target:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
            return await original_get(url)

        source.get_chapter = delayed
        loading = asyncio.create_task(reader.prefetch())
        for _ in range(4):
            await asyncio.sleep(0)
        source.search_pages["new", None] = Page([source.works["E"].work], "new")
        await reader.search(SearchFilters("new"))
        await reader.open_result(0)
        gate.set()
        await loading
        assert reader.current.id == "E1"
        assert reader.filters.query == "new"
        assert reader.prefetch_status["ready"] == 0
        assert reader.prefetch_status["state"] == "idle"

    asyncio.run(run())


def test_prefetch_fetch_budget_keeps_the_first_unchecked_candidate_for_next():
    async def run():
        source = FixtureSource()
        first = source.add_work("A")
        gated = [source.add_work(str(i), rating="Not Rated") for i in range(9)]
        for w in gated[:7]:
            source.fail["work", w.url] = error("adult_confirmation")
        source.recent_pages[None] = Page([first] + gated, "recent1")
        reader = engine(source)
        await reader.start()
        before = len(source.calls)
        await reader.prefetch()
        assert len(source.calls) - before <= 24
        # Seven unavailable candidates use seven requests; the next readable
        # candidate may be fetched, but a later untested candidate must not vanish.
        read = []
        for _ in range(4):
            chapter = await reader.next()
            if chapter:
                read.append(chapter.work.id)
            if "8" in read:
                break
        assert read[:2] == ["7", "8"]

    asyncio.run(run())


def test_search_draft_is_distinct_from_submitted_filters_and_survives_restart():
    async def run():
        source = fixture_flow()
        store = MemoryStore()
        reader = engine(source, store)
        await reader.search(SearchFilters("rain", language="en"))
        await reader.open_result(0)
        reader.search_draft = {"query": "unsubmitted snow", "language": "zh", "warnings": ["16"]}
        reader.save()
        restored = engine(fixture_flow(), store)
        await restored.start()
        assert restored.filters.query == "rain" and restored.filters.language == "en"
        assert restored.search_draft["query"] == "unsubmitted snow"
        assert restored.search_draft["language"] == "zh"

    asyncio.run(run())


def test_close_clears_cached_bodies_but_does_not_close_caller_owned_store():
    class OwnedStore(MemoryStore):
        closed = False
        cleared = False

        def clear_cache(self):
            self.cache.clear()
            self.cleared = True

        def close(self):
            self.closed = True

    async def run():
        store = OwnedStore()
        reader = engine(fixture_flow(), store)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        await reader.prefetch()
        assert store.cache
        await reader.close()
        assert store.cleared and not store.cache and not store.closed
        assert store.state["current"]["id"] == "A1"
        assert len(store.state["history"]) == 1

    asyncio.run(run())


def test_restored_result_page_can_go_back_then_forward_with_its_selection():
    async def run():
        source = fixture_flow()
        source.search_pages["rain", "search1"] = copy.deepcopy(source.search_pages["rain", None])
        store = MemoryStore()
        reader = engine(source, store)
        await reader.search(SearchFilters("rain"))
        await reader.open_result(0)
        await reader.next_results()
        reader.set_result_selection(1)
        reader.save()
        restored = engine(source, store)
        await restored.start()
        assert restored.results_page_number == 2 and restored.results_selection == 1
        assert [w.id for w in (await restored.prev_results()).items] == ["A", "C"]
        assert restored.results_page_number == 1
        calls = len(source.calls)
        assert [w.id for w in (await restored.next_results()).items] == ["B", "D"]
        assert restored.results_selection == 1 and len(source.calls) == calls
        assert restored.current.id == "A1"

    asyncio.run(run())


def test_engine_marks_speculation_background_and_promotes_the_same_pending_task():
    from contextlib import asynccontextmanager

    class PrioritySource(FixtureSource):
        def __init__(self):
            super().__init__()
            self.priorities = []
            self.promoted = []

        @asynccontextmanager
        async def request_context(self, priority="foreground"):
            self.priorities.append(priority)
            yield

        def promote(self, task):
            self.promoted.append(task)
            return True

    async def run():
        source = PrioritySource()
        a = source.add_work("A", 5)
        source.recent_pages[None] = Page([a], "recent1")
        reader = engine(source)
        await reader.start()
        assert source.priorities == ["foreground", "foreground"]
        gate = asyncio.Event()
        source.gates["chapter", reader.current.next_url] = gate
        loading = asyncio.create_task(reader.prefetch())
        for _ in range(4):
            await asyncio.sleep(0)
        next_task = asyncio.create_task(reader.next())
        for _ in range(4):
            await asyncio.sleep(0)
        assert source.promoted
        assert all(task is source.promoted[0] for task in source.promoted)
        gate.set()
        assert (await next_task).id == "A2"
        await loading
        # Consuming chapter 2 while preparation is running replenishes chapter
        # 5, leaving the rolling three-chapter window ready at the new position.
        assert source.priorities[2:] == ["background"] * 4
        assert reader.prefetch_status["current_work_ready"] == 3
        assert source.calls.count(("chapter", source.works["A"].chapters[1].url)) == 1

    asyncio.run(run())


def test_real_source_parser_and_scheduler_prefetch_three_chapters_without_more_http():
    """Only HTTP is substituted; AO3 parsing, priority scheduling and engine run."""
    import httpx

    from mini_notes.source import AO3Source

    paths = []

    def handler(request):
        paths.append(request.url.path)
        if "/chapters/" not in request.url.path:
            return httpx.Response(200, text='<div id="main"><ol class="work index"></ol></div>')
        number = int(request.url.path.rsplit("/", 1)[1])
        following = (
            f'<li class="chapter next"><a href="/works/101/chapters/{number + 1}">'
            "Next Chapter</a></li>"
            if number < 1003
            else ""
        )
        html = (
            f'<div id="main"><ul>{following}</ul><div id="workskin">'
            '<dl class="work meta"><dd class="rating tags">General Audiences</dd>'
            '<dd class="warning tags">No Archive Warnings Apply</dd>'
            '<dd class="category tags">Gen</dd><dd class="language">中文-普通话 國語</dd>'
            '<dd class="chapters">4/4</dd></dl><div class="preface">'
            '<h2 class="title">原创四章测试</h2><h3 class="byline">原创作者</h3></div>'
            '<div id="chapters"><div class="chapter"><div class="preface">'
            f'<h3 class="title">原创章 {number}</h3></div><div class="userstuff" '
            f'role="article"><p>仅测试使用的自拟正文 {number}。</p></div></div></div></div></div>'
        )
        return httpx.Response(200, text=html)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = AO3Source("https://reader.example", client=client, min_interval=0, max_retries=0)
        reader = engine(source)
        reader.filters = SearchFilters(language="zh")
        await reader.open_url("https://reader.example/works/101/chapters/1000")
        await reader.prefetch()
        assert reader.prefetch_status["ready"] == 3
        assert [path for path in paths if "/chapters/" in path] == [
            f"/works/101/chapters/{i}" for i in range(1000, 1004)
        ]
        before = len(paths)
        for number in range(1001, 1004):
            assert (await reader.next()).paragraphs == [f"仅测试使用的自拟正文 {number}。"]
        assert len(paths) == before
        await reader.close()
        await client.aclose()

    asyncio.run(run())


def test_language_change_restores_body_but_restarts_original_keyword_in_new_language():
    from mini_notes.cli import prepare_language

    async def run():
        source = fixture_flow()
        store = MemoryStore()
        reader = engine(source, store)
        await reader.search(SearchFilters("rain", language="en"))
        await reader.open_result(0)
        reader.set_position({"paragraph": 1, "offset": 2})
        reader.save()
        store.state["flow"]["skipped"] = ["Z"]  # Previously rejected by the old language.
        assert prepare_language(store, {"language": "en"}, "zh") == "zh"
        chinese = source.add_work("Z", language="中文-普通话 國語", language_id="zh")
        source.search_pages["rain", None] = Page([chinese], "new-zh-search")
        restored = engine(source, store)
        calls = len(source.calls)
        await restored.start()
        assert restored.current.id == "A1" and len(source.calls) == calls
        assert restored.position == {"paragraph": 1, "offset": 2}
        assert (await restored.next()).id == "Z1"
        assert source.calls[calls:] == [("search", ("rain", None)), ("work", chinese.url)]
        assert restored.filters.language == "zh"
        assert restored._flow.origin.filters.query == "rain"

    asyncio.run(run())
