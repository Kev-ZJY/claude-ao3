"""Reading a long work must take priority over unrelated discovery."""

import asyncio

import pytest

from mini_notes.models import Page, SearchFilters
from mini_notes.source import SourceError
from test_engine import FixtureSource, engine


def long_work_reader():
    source = FixtureSource()
    current = source.add_work("A", 28)
    other = source.add_work("B", 4)
    source.search_pages["rain", None] = Page([current, other], "search1")
    source.recent_pages[None] = Page([], "recent1")
    return source, engine(source)


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_three_next_chapters_are_ready_before_slow_other_work_discovery():
    source, reader = long_work_reader()
    await reader.search(SearchFilters("rain"))
    await reader.open_result(0)
    other = source.works["B"].work.url
    source.gates["work", other] = asyncio.Event()
    loader = asyncio.create_task(reader.prefetch())
    try:
        await wait_until(lambda: ("work", other) in source.calls)
        assert reader.prefetch_status["current_work_ready"] == 3
        for number in (2, 3, 4):
            assert reader.store.cache_get("chapter:" + source.works["A"].chapters[number - 1].url)
    finally:
        await reader.close()
        await loader


@pytest.mark.asyncio
async def test_advancing_replenishes_next_chapters_without_waiting_for_other_work():
    source, reader = long_work_reader()
    await reader.search(SearchFilters("rain"))
    await reader.open_result(0)
    other = source.works["B"].work.url
    source.gates["work", other] = asyncio.Event()
    loader = asyncio.create_task(reader.prefetch())
    try:
        await wait_until(lambda: ("work", other) in source.calls)
        assert (await asyncio.wait_for(reader.next(), .2)).id == "A2"
        # No extra prefetch() call: the running preparation must follow the
        # reader and fetch chapter 5 even while the unrelated work never replies.
        chapter5 = source.works["A"].chapters[4].url
        await wait_until(lambda: reader.store.cache_get("chapter:" + chapter5) is not None)
        assert reader.prefetch_status["current_work_ready"] == 3
        assert reader.current.id == "A2"
        assert source.calls.count(("chapter", source.works["A"].chapters[1].url)) == 1
        for expected in ("A3", "A4", "A5"):
            assert (await asyncio.wait_for(reader.next(), .2)).id == expected
    finally:
        await reader.close()
        await loader


@pytest.mark.asyncio
async def test_unrelated_late_response_cannot_publish_after_read_ahead_reprioritizes():
    source, reader = long_work_reader()
    await reader.search(SearchFilters("rain"))
    await reader.open_result(0)
    other = source.works["B"].work.url
    started, release = asyncio.Event(), asyncio.Event()
    original = source.get_work

    async def cancellation_resistant(url):
        if url == other:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        return await original(url)

    source.get_work = cancellation_resistant
    loader = asyncio.create_task(reader.prefetch())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert (await reader.next()).id == "A2"
        chapter5 = source.works["A"].chapters[4].url
        await wait_until(lambda: reader.store.cache_get("chapter:" + chapter5) is not None)
        # Foreground state and prepared chapter order survive a response from
        # the old chapter's background discovery after cancellation.
        reader.pause_prefetch()
        release.set()
        await loader
        assert reader.current.id == "A2"
        assert reader.store.cache_get("work:" + other) is None
        assert (await reader.next()).id == "A3"
    finally:
        release.set()
        await reader.close()
        await loader


@pytest.mark.asyncio
@pytest.mark.parametrize("action,initial,blocked,expected", [
    ("skip_work", 1, 3, "B1"),
    ("previous", 10, 12, "A9"),
])
async def test_explicit_navigation_does_not_wait_for_unrelated_deep_read_ahead(
    action, initial, blocked, expected
):
    source, reader = long_work_reader()
    await reader.search(SearchFilters("rain"))
    if initial == 1:
        await reader.open_result(0)
    else:
        await reader.open_url(source.works["A"].chapters[initial - 1].url)
    blocked_url = source.works["A"].chapters[blocked - 1].url
    source.gates["chapter", blocked_url] = asyncio.Event()
    loader = asyncio.create_task(reader.prefetch())
    try:
        await wait_until(lambda: ("chapter", blocked_url) in source.calls)
        chapter = await asyncio.wait_for(getattr(reader, action)(), .2)
        assert chapter.id == expected
        assert reader.current.id == expected
    finally:
        await reader.close()
        await loader


@pytest.mark.asyncio
@pytest.mark.parametrize("chapter_count", [1, 28])
async def test_skip_at_last_chapter_reuses_pending_next_work_request(chapter_count):
    source = FixtureSource()
    source.add_work("A", chapter_count)
    other = source.add_work("B", 4)
    source.recent_pages[None] = Page([other], "recent1")
    reader = engine(source)
    await reader.open_url(source.works["A"].chapters[-1].url)
    release = asyncio.Event()
    source.gates["work", other.url] = release
    loader = asyncio.create_task(reader.prefetch())
    skipping = None
    try:
        await wait_until(lambda: ("work", other.url) in source.calls)
        skipping = asyncio.create_task(reader.skip_work())
        await asyncio.sleep(0)
        release.set()
        assert (await asyncio.wait_for(skipping, 1)).id == "B1"
        assert source.calls.count(("work", other.url)) == 1
    finally:
        release.set()
        await reader.close()
        await loader
        if skipping:
            await asyncio.gather(skipping, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds_left", [0, 30])
async def test_next_work_cooldown_survives_same_work_window_replenishment(monkeypatch, seconds_left):
    source = FixtureSource()
    current = source.add_work("A", 3)
    other = source.add_work("B", 3)
    source.recent_pages[None] = Page([other], "recent1")
    source.fail["work", other.url] = SourceError(
        "rate_limited", "原创限流模拟", retry_after=120, http_status=429
    )
    reader = engine(source)
    try:
        await reader.open_url(current.url)
        await reader.prefetch()
        assert (await reader.next()).id == "A2"
        await reader.prefetch()
        now = reader._prefetch_cooldown_until - seconds_left + (0 if seconds_left else 1)
        with monkeypatch.context() as scoped:
            scoped.setattr("mini_notes.engine.time.monotonic", lambda: now)
            with pytest.raises(SourceError) as exc:
                await reader.skip_work()
            assert exc.value.retry_after == pytest.approx(seconds_left, abs=.01)
        assert source.calls.count(("work", other.url)) == 1
        assert reader.current.id == "A2"
        assert (await reader.next()).id == "A3"
    finally:
        await reader.close()
