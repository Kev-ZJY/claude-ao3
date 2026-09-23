"""Isolated network scheduling regressions; no external requests or user state."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest

from mini_notes.models import Page, SearchFilters
from mini_notes.source import SourceError, _RequestGate
from test_engine import FixtureSource, engine


def test_next_can_progress_while_unrelated_branch_is_pending_without_losing_branch():
    async def run():
        source = FixtureSource()
        a = source.add_work("A", 6)
        works = [source.add_work(key, 3) for key in "BCD"]
        source.recent_pages[None] = Page(works, "recent")
        reader = engine(source)
        await reader.open_url(a.url)
        gate = asyncio.Event()
        source.gates["work", works[0].url] = gate
        loader = asyncio.create_task(reader.prefetch())
        try:
            for _ in range(12):
                await asyncio.sleep(0)
            assert reader.prefetch_status["ready"] == 3
            assert (await reader.next()).id == "A2"
            assert (await asyncio.wait_for(reader.next(), .1)).id == "A3"
            assert not loader.done()  # Preparation follows the new reading position.
            gate.set()
            await loader
            assert reader.prefetch_status["ready"] == 3
            assert {p["work_id"] for p in reader.prefetch_status["work_branches"]} == set("BCD")
            assert all(p["ready"] == 3 for p in reader.prefetch_status["work_branches"])
            assert (await reader.next()).id == "A4"  # No stale A3 reappears.
            assert source.calls.count(("chapter", a.url + "/chapters/A3")) == 1
        finally:
            gate.set()
            await loader
            await reader.close()

    asyncio.run(run())


@pytest.mark.parametrize("action,seconds_left", [("next", 0), ("next", 30), ("skip_work", 0)])
def test_background_retry_after_only_exposes_remaining_time(monkeypatch, action, seconds_left):
    async def run():
        source = FixtureSource()
        a = source.add_work("A", 3)
        reader = engine(source)
        await reader.open_url(a.url)
        target = ("chapter", reader.current.next_url)
        if action == "skip_work":
            b = source.add_work("B", 3)
            source.recent_pages[None] = Page([b], "recent")
            target = ("work", b.url)
        source.fail[target] = SourceError(
            "rate_limited", "原创限流模拟", retry_after=120, http_status=429
        )
        await reader.prefetch()
        now = reader._prefetch_cooldown_until - seconds_left + (0 if seconds_left else 1)
        with monkeypatch.context() as scoped:
            scoped.setattr("mini_notes.engine.time.monotonic", lambda: now)
            assert reader.prefetch_status["cooldown"] == bool(seconds_left)
            try:
                await getattr(reader, action)()
            except SourceError as exc:
                assert exc.http_status == 429
                assert exc.retry_after == pytest.approx(seconds_left, abs=.01)
            else:
                raise AssertionError("Cached source error should still be visible")
        await reader.close()

    asyncio.run(run())


def test_foreground_next_queues_before_later_branches_with_one_source_slot():
    class SerialSource(FixtureSource):
        def __init__(self):
            super().__init__()
            self.gate = _RequestGate()
            self.priority = ContextVar("fixture_priority", default="foreground")
            self.active = self.peak = 0

        @asynccontextmanager
        async def request_context(self, *, priority):
            token = self.priority.set(priority)
            try:
                yield
            finally:
                self.priority.reset(token)

        def promote(self, task):
            return self.gate.promote(task)

        async def _before(self, method, key):
            async with self.gate.slot(self.priority.get()):
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await super()._before(method, key)
                finally:
                    self.active -= 1

    async def run():
        source = SerialSource()
        a = source.add_work("A", 6)
        works = [source.add_work(key, 3) for key in "BCD"]
        source.recent_pages[None] = Page(works, "recent")
        reader = engine(source)
        await reader.open_url(a.url)
        gate = asyncio.Event()
        source.gates["work", works[0].url] = gate
        loader = asyncio.create_task(reader.prefetch())
        try:
            for _ in range(12):
                await asyncio.sleep(0)
            assert (await reader.next()).id == "A2"
            foreground = asyncio.create_task(reader.next())
            for _ in range(6):
                await asyncio.sleep(0)
            assert foreground.done()  # A3 is cached; the slow B request must yield.
            gate.set()
            assert (await asyncio.wait_for(foreground, 1)).id == "A3"
            await loader
            assert source.peak == 1
            assert source.calls.index(("chapter", a.url + "/chapters/A3")) < source.calls.index(("work", works[1].url))
        finally:
            gate.set()
            await loader
            await reader.close()

    asyncio.run(run())


def test_matching_inflight_branch_is_shared_by_foreground_without_duplicate_fetch():
    async def run():
        source = FixtureSource()
        a, b = source.add_work("A", 2), source.add_work("B", 4)
        source.recent_pages[None] = Page([b], "recent")
        reader = engine(source)
        await reader.open_url(a.url)
        gate = asyncio.Event()
        source.gates["work", b.url] = gate
        loader = asyncio.create_task(reader.prefetch())
        try:
            for _ in range(12):
                await asyncio.sleep(0)
            assert (await reader.next()).id == "A2"
            foreground = asyncio.create_task(reader.next())
            for _ in range(6):
                await asyncio.sleep(0)
            assert source.calls.count(("work", b.url)) == 1
            gate.set()
            assert (await asyncio.wait_for(foreground, 1)).id == "B1"
            await loader
            assert source.calls.count(("work", b.url)) == 1
            assert (await reader.next()).id == "B2"
        finally:
            gate.set()
            await loader
            await reader.close()

    asyncio.run(run())


def test_cancel_foreground_invalidates_late_search_without_replacing_committed_state():
    async def run():
        source = FixtureSource()
        a, b = source.add_work("A"), source.add_work("B")
        source.search_pages["old", None] = Page([a], "old")
        source.search_pages["new", None] = Page([b], "new")
        reader = engine(source)
        await reader.search(SearchFilters("old"))
        await reader.open_result(0)
        reader.set_position({"paragraph": 1, "offset": 2})
        reader.search_draft = {"query": "new"}
        gate, entered = asyncio.Event(), asyncio.Event()
        original = source.search

        async def cancellation_resistant(**kwargs):
            entered.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()
            return await original(**kwargs)

        source.search = cancellation_resistant
        task = asyncio.create_task(reader.search(SearchFilters("new")))
        await entered.wait()
        task.cancel()
        reader.cancel_foreground()
        await asyncio.sleep(0)
        gate.set()
        await task
        assert reader.filters.query == "old" and reader.results.url == "old"
        assert reader.current.id == "A1" and reader.position == {"paragraph": 1, "offset": 2}
        assert reader.search_draft == {"query": "new"} and reader.prefetch_status["paused"]
        await reader.close()

    asyncio.run(run())
