"""Headless native-TUI behavior, using deterministic source-independent chapters."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Input, Tabs

from mini_notes.app import MiniNotesApp, ReaderViewport, SearchSelection, WorkMask
from mini_notes.models import Chapter, Page, SearchFilters, WorkSummary
from mini_notes.source import SourceError


def chapter(number=1):
    work = WorkSummary(id='10', url='https://archiveofourown.org/works/10', title='Fixture title', authors=['Fixture'], chapter_count=2)
    return Chapter(id=str(number), url=f'https://archiveofourown.org/works/10/chapters/{number}', title=f'Chapter {number}', paragraphs=[f'第 {i} 段，测试真实终端布局。The office had a quiet afternoon. '+ '雨水落在窗外，文字保持原来的顺序。'*5 for i in range(40)], work=work, position=number)


class FakeEngine:
    def __init__(self):
        self.current = None
        self.results = None
        self.filters = SearchFilters()
        self.position = {}
        self.source_label = 'official'
        self.notice = ''
        self.source = SimpleNamespace(base_url='https://archiveofourown.org')
        self.start_gate = None
        self.next_gate = None
        self.fail_next = None
        self.closed = False
        self.calls = []

    async def start(self):
        if self.start_gate:
            await self.start_gate.wait()
        self.current = chapter()
        return self.current

    def set_position(self, anchor):
        self.position = dict(anchor)

    async def prefetch(self):
        self.calls.append('prefetch')

    async def search(self, filters):
        self.filters = filters
        self.calls.append('search')
        self.results = Page([chapter().work], 'https://archiveofourown.org/works/search', 'https://archiveofourown.org/works/search?page=2')
        return self.results

    async def more_results(self):
        work = WorkSummary(id='20', url='https://archiveofourown.org/works/20', title='Second fixture')
        self.results = Page(self.results.items+[work], 'https://archiveofourown.org/works/search?page=2')
        return self.results

    async def open_result(self, index):
        self.calls.append(('open', index))
        self.current = chapter(1)
        self.position = {}
        return self.current

    async def next(self):
        self.calls.append('next')
        if self.next_gate:
            await self.next_gate.wait()
        if self.fail_next:
            raise self.fail_next
        self.current = chapter(2)
        self.position = {}
        return self.current

    async def previous(self):
        self.calls.append('previous')
        self.current = chapter(1)
        self.position = {}
        return self.current

    async def close(self):
        self.closed = True

    async def skip(self):
        self.calls.append('skip')
        self.fail_next = None
        return await self.next()


async def settled(pilot):
    await pilot.pause()
    await asyncio.sleep(.01)
    await pilot.pause()


@pytest.mark.parametrize("close_fails", [False, True])
async def test_close_failure_exits_with_nonzero_status(close_fails):
    from mini_notes.storage import StorageError

    engine = FakeEngine()

    async def close():
        engine.closed = True
        if close_fails:
            raise StorageError("synthetic save failure")

    engine.close = close
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        await pilot.press("ctrl+c")
        await settled(pilot)
    assert engine.closed
    assert app.return_code == (2 if close_fails else 0)


@pytest.mark.asyncio
async def test_preparation_finishing_after_navigation_starts_for_the_new_chapter():
    engine = FakeEngine()
    release, first_started, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    prepared = []

    async def prefetch():
        prepared.append(engine.current.id)
        if len(prepared) == 1:
            first_started.set()
            await release.wait()
        else:
            second_started.set()

    engine.prefetch = prefetch
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await asyncio.wait_for(first_started.wait(), 1)
        app.navigate(1)
        await settled(pilot)
        assert engine.current.id == "2" and not app.busy
        release.set()
        await asyncio.wait_for(second_started.wait(), 1)
        assert prepared == ["1", "2"]


@pytest.mark.asyncio
async def test_cold_start_mask_stays_opaque_when_request_completes():
    engine = FakeEngine()
    engine.start_gate = asyncio.Event()
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press('ctrl+g')
        assert app.masked and isinstance(app.screen, WorkMask)
        engine.start_gate.set()
        await settled(pilot)
        assert app.masked and isinstance(app.screen, WorkMask)
        assert engine.current is not None
        assert 'Fixture title' not in '\n'.join(app.screen.query_one('#mask-body', ReaderViewport).paragraphs)
        assert app.screen.query_one('#mask-footer').size.height == 6
        await pilot.press('ctrl+g')
        await settled(pilot)
        assert not app.masked and app.focused is app.reader
        assert app.reader.paragraphs == tuple(engine.current.paragraphs)
        await pilot.press('ctrl+c')
        assert engine.closed


@pytest.mark.asyncio
async def test_search_space_multiselect_results_and_return_progress():
    engine = FakeEngine()
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.reader.move(15)
        anchor, percent = app.reader.anchor(), app.reader.percent
        app.open_search()
        await pilot.press('r', 'a', 'i', 'n', 'space', 'd', 'a', 'y')
        assert app.main_query('#any-field', Input).value == 'rain day'
        assert app.reader.percent == percent
        app.main_query('#search-tabs', Tabs).active = 'tab-warnings'
        await settled(pilot)
        assert isinstance(app.focused, SearchSelection)
        await pilot.press('space')
        assert app.main_query('#warning-options', SearchSelection).selected == ['14']
        app.main_query('#search-tabs', Tabs).active = 'tab-categories'
        await settled(pilot)
        await pilot.press('down', 'down', 'space', 'enter')
        await settled(pilot)
        assert engine.filters.query == 'rain day'
        assert engine.filters.warnings == ['14']
        assert engine.filters.categories == ['21']
        assert app.mode == 'results'
        assert app.reader.anchor() == anchor
        await pilot.press('n')
        await settled(pilot)
        assert len(engine.results.items) == 2
        await pilot.press('enter')
        await settled(pilot)
        assert ('open', 0) in engine.calls
        assert app.mode == 'reader'


@pytest.mark.asyncio
async def test_draft_scroll_focus_and_error_survive_mask():
    engine = FakeEngine()
    app = MiniNotesApp(engine)
    async with app.run_test(size=(74, 20)) as pilot:
        await settled(pilot)
        app.reader.move(23)
        anchor = app.reader.anchor()
        app.focus_command('/search unfinished draft')
        await pilot.press('ctrl+g')
        await pilot.press('ctrl+g')
        await settled(pilot)
        assert app.focused is app.main_query('#command')
        assert app.main_query('#command', Input).value == '/search unfinished draft'
        assert app.reader.anchor() == anchor
        engine.next_gate = asyncio.Event()
        engine.fail_next = SourceError('forbidden', 'HTTP 403')
        app.navigate(1)
        await pilot.press('ctrl+g')
        engine.next_gate.set()
        await settled(pilot)
        assert app.mode == 'error' and app.masked
        await pilot.press('ctrl+g')
        await settled(pilot)
        assert app.focused is app.main_query('#error-back')
        assert app.main_query('#command', Input).value == '/search unfinished draft'
        assert app.reader.anchor() == anchor
        await pilot.press('enter')
        await settled(pilot)
        assert app.mode == 'reader'
        assert app.reader.anchor() == anchor


@pytest.mark.asyncio
async def test_resize_cached_viewport_theme_and_short_terminal():
    engine = FakeEngine()
    app = MiniNotesApp(engine, theme_mode='light')
    async with app.run_test(size=(110, 30)) as pilot:
        await settled(pilot)
        app.reader.move(40)
        await settled(pilot)
        count = app.reader.layout_count
        for _ in range(8):
            app.reader.move(1)
        await settled(pilot)
        assert app.reader.layout_count == count
        saved = app.reader.anchor()
        await pilot.resize_terminal(48, 15)
        await settled(pilot)
        current = app.reader.anchor()
        assert current['paragraph'] == saved['paragraph']
        assert current['offset'] <= saved['offset']
        assert app.reader.size.height == 9
        assert app.main_query('#main-footer').size.height == 6
        assert app.theme == 'mini-light' and not app.dark_palette
        app.set_color_mode('dark')
        assert app.theme == 'mini-dark' and app.dark_palette
        app.open_search()
        await pilot.press('ctrl+g', 'ctrl+g')
        await settled(pilot)
        assert app.mode == 'search'
        assert app.focused is app.main_query('#any-field')


@pytest.mark.asyncio
async def test_boundary_navigation_requires_separate_action():
    engine = FakeEngine()
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.reader.action_end()
        app.reader._last_key = 0
        app.reader.action_step(1)
        await settled(pilot)
        assert engine.calls.count('next') == 1
        app.reader.action_end()
        # Model a key-repeat packet, not an independent new press.
        import time
        app.reader._last_key = time.monotonic()
        app.reader.action_step(1)
        await settled(pilot)
        assert engine.calls.count('next') == 1


@pytest.mark.asyncio
async def test_warning_search_short_window_and_mask_hides_entire_private_view():
    engine = FakeEngine()
    app = MiniNotesApp(engine)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        assert app.reader.percent == 0
        app.open_search()
        app.main_query('#any-field', Input).value = 'PRIVATE_SEARCH_QUERY'
        app.main_query('#search-tabs', Tabs).active = 'tab-warnings'
        await settled(pilot)
        options = app.main_query('#warning-options', SearchSelection)
        assert options.size.height >= 3
        await pilot.press('end', 'space')
        selected = list(options.selected)
        app.main_query('#command', Input).value = 'PRIVATE_COMMAND_DRAFT'
        await pilot.press('ctrl+g')
        await settled(pilot)
        text = '\n'.join(strip.text for strip in app.screen._compositor.render_strips())
        assert 'PRIVATE' not in text and 'Warnings' not in text and 'Fixture' not in text
        await pilot.press('ctrl+g')
        await settled(pilot)
        assert app.focused is options and options.selected == selected
        assert app.main_query('#any-field', Input).value == 'PRIVATE_SEARCH_QUERY'


@pytest.mark.asyncio
async def test_wheel_inertia_stops_at_boundary_until_fresh_gesture():
    engine = FakeEngine()
    app = MiniNotesApp(engine)
    event = SimpleNamespace(stop=lambda: None, prevent_default=lambda: None)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.reader.move(app.reader.max_offset-1)
        app.reader._wheel(event, 1)
        app.reader._wheel(event, 1)
        await settled(pilot)
        assert engine.calls.count('next') == 0
        app.reader._last_wheel = 0
        app.reader._wheel(event, 1)
        await settled(pilot)
        assert engine.calls.count('next') == 1
        app.reader.action_end()
        app.reader._wheel(event, 1)
        await settled(pilot)
        assert engine.calls.count('next') == 1


@pytest.mark.asyncio
async def test_native_flow_with_real_engine_and_source_boundary_fixture():
    from test_engine import FixtureSource, MemoryStore
    from mini_notes.engine import ReaderEngine

    source = FixtureSource()
    work = source.add_work('10', count=2, language='中文-普通话 國語', language_id='zh')
    source.recent_pages[None] = Page([work], 'https://reader.example/recent')
    source.search_pages['rain', None] = Page([work], 'https://reader.example/search')
    store = MemoryStore()
    engine = ReaderEngine(source, store)
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        assert engine.current.id == '101'
        app.focus_command('/search')
        await pilot.press('enter')
        app.main_query('#any-field', Input).value = 'rain'
        await pilot.press('enter')
        await settled(pilot)
        assert app.mode == 'results' and len(engine.results.items) == 1
        await pilot.press('enter')
        await settled(pilot)
        assert engine.current.id == '101'
        app.navigate(1)
        await settled(pilot)
        assert engine.current.id == '102'
        app.navigate(-1)
        await settled(pilot)
        assert engine.current.id == '101'
        await pilot.press('ctrl+c')
        assert source.closed and store.state['current']['id'] == '101'


@pytest.mark.asyncio
async def test_retired_commands_preserve_reading_without_selecting_series():
    from mini_notes.models import SeriesRef
    engine = FakeEngine()
    engine.series = [SeriesRef('7', 'https://archiveofourown.org/series/7', 'Fixture sequence')]
    engine.selected_series = None
    async def choose(url):
        engine.selected_series = url
    engine.select_series = choose
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        original = app.reader.paragraphs
        app.focus_command('/series 1')
        await pilot.press('enter')
        await settled(pilot)
        assert engine.selected_series is None
        assert app.mode == 'reader'
        assert app.reader.paragraphs == original
        app.execute_command('/title')
        assert app.reader.paragraphs == original and not app.reader.document_title
        app.focus_command('/')
        await settled(pilot)
        assert not {'/title', '/series'} & set(app.command_matches)


@pytest.mark.asyncio
async def test_persisted_filters_restore_into_search_form_after_engine_start():
    from test_engine import FixtureSource, MemoryStore
    from mini_notes.engine import ReaderEngine

    source, store = FixtureSource(), MemoryStore()
    work = source.add_work('10', language='中文-普通话 國語', language_id='zh')
    source.recent_pages[None] = Page([work], 'https://reader.example/recent')
    source.search_pages['rain day', None] = Page([work], 'https://reader.example/search')
    original = ReaderEngine(source, store)
    await original.start()
    await original.search(SearchFilters('rain day', ['16'], ['21']))
    original.save()
    restored = ReaderEngine(source, store)
    app = MiniNotesApp(restored)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        assert restored.filters.query == 'rain day'
        app.open_search()
        assert app.main_query('#any-field', Input).value == 'rain day'
        assert app.main_query('#warning-options', SearchSelection).selected == ['16']
        assert app.main_query('#category-options', SearchSelection).selected == ['21']
        app.main_query('#any-field', Input).value = 'new unfinished draft'
        await pilot.press('escape')
        app.open_search()
        assert app.main_query('#any-field', Input).value == 'new unfinished draft'


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", " 已跳过不匹配或需确认访问的作品。"])
async def test_exhaustion_toast_expires_once_without_erasing_progress_or_errors(suffix):
    from textual.widgets import Static
    engine = FakeEngine()
    app = MiniNotesApp(engine)
    app.TOAST_SECONDS = .15
    toast = '搜索结果已浏览完毕，正在为您推荐近期新作' + suffix
    async with app.run_test(size=(100, 24)) as pilot:
        await settled(pilot)
        engine.notice = toast
        app.update_footer()
        assert toast in str(app.main_query('#above-input', Static).content)
        initial_progress = str(app.main_query('#footer-first', Static).content)
        for _ in range(5):
            await asyncio.sleep(.05)
            app.update_footer()
        await pilot.pause()
        assert toast not in str(app.main_query('#above-input', Static).content)
        assert str(app.main_query('#footer-first', Static).content) == initial_progress
        # An old timer must not clear a later error or a network progress label.
        engine.notice = ''
        app.update_footer()
        engine.notice = toast
        app.update_footer()
        app._local_notice = '正在打开作品…'
        await asyncio.sleep(.2)
        await pilot.pause()
        assert str(app.main_query('#above-input', Static).content) == '正在打开作品…'
        app.show_error(SourceError('forbidden', 'HTTP 403'))
        app.update_footer()
        assert app.mode == 'error'
        assert '草稿已保留' in str(app.main_query('#above-input', Static).content)


@pytest.mark.asyncio
async def test_slash_command_entry_keeps_prefix_when_focus_changes():
    app = MiniNotesApp(FakeEngine())
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        assert app.focused is app.reader
        await pilot.press('/')
        await pilot.press(*'search')
        assert app.main_query('#command', Input).value == '/search'
        await pilot.press('enter')
        assert app.mode == 'search'
        assert app.focused is app.main_query('#any-field')


@pytest.mark.asyncio
@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('no_color', [False, True])
async def test_command_background_and_accent_respect_theme_and_no_color(monkeypatch, theme, no_color):
    if no_color:
        monkeypatch.setenv('NO_COLOR', '1')
    else:
        monkeypatch.delenv('NO_COLOR', raising=False)
    app = MiniNotesApp(FakeEngine(), theme_mode=theme)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        strips = app.screen._compositor.render_strips()
        command_segment = next(segment for segment in strips[0] if 'section' in segment.text)
        body_segment = next(segment for segment in strips[2] if '第' in segment.text)
        if no_color:
            assert command_segment.style.reverse and not body_segment.style.reverse
            assert command_segment.style.bgcolor.is_default
        else:
            assert command_segment.style.bgcolor != body_segment.style.bgcolor
            assert body_segment.style.color != body_segment.style.bgcolor
        colored = [segment.style.color.get_truecolor() for strip in strips[:4] for segment in strip if segment.text.strip() and segment.style.color]
        if no_color:
            assert all(rgb.red == rgb.green == rgb.blue for rgb in colored)
        else:
            assert any(not (rgb.red == rgb.green == rgb.blue) for rgb in colored)
        app.reader.set_document(['The office had a quiet afternoon. The clock said 14:30.'])
        await pilot.pause()
        # English prose remains in the base foreground; only the number receives accent.
        line = app.reader.render_line(2)
        plain = next(segment for segment in line if 'office' in segment.text)
        number = next(segment for segment in line if '14:30' in segment.text)
        assert plain.style.color != number.style.color


@pytest.mark.asyncio
async def test_auto_theme_inherits_host_background_and_compact_search_actions():
    app = MiniNotesApp(FakeEngine())
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        assert next(iter(app.reader.render_line(2))).style.bgcolor.is_default
        app.open_search()
        await settled(pilot)
        assert app.main_query('#search-submit').size.height == 1
        assert app.main_query('#search-buttons').region.bottom <= app.main_query('#main-footer').region.y


@pytest.mark.asyncio
async def test_slash_menu_filters_and_preserves_menu_focus_under_mask():
    engine = FakeEngine()
    engine.series = [object()]
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        await pilot.press('/')
        assert app.main_query('#command-menu').display
        await pilot.press(*'skip-')
        assert app.command_matches == ['/skip-work']
        await pilot.press('down', 'ctrl+g', 'ctrl+g')
        assert app.main_query('#command-menu').highlighted == 0
        assert app.main_query('#command', Input).value == '/skip-'
        assert app.main_query('#command-menu').display
        await pilot.press('escape')
        assert not app.main_query('#command-menu').display


@pytest.mark.asyncio
async def test_removed_open_command_advises_search_without_navigation():
    app = MiniNotesApp(FakeEngine())
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        app.focus_command('/open https://archiveofourown.org/works/1')
        await pilot.press('enter')
        assert not app.command_menu_open
        assert app.mode == 'reader' and not app.busy
        assert '/search' in app._local_notice


@pytest.mark.asyncio
async def test_search_language_and_reading_key_hints():
    from textual.widgets import Static
    engine = FakeEngine()
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        assert 'n 下一' in str(app.main_query('#footer-second', Static).content)
        app.open_search()
        await pilot.press('enter')
        await settled(pilot)
        assert engine.filters.language == 'zh'


@pytest.mark.asyncio
async def test_true_result_pages_previous_selection_and_open_restore():
    from test_engine import FixtureSource, MemoryStore
    from mini_notes.engine import ReaderEngine
    from mini_notes.app import ResultOptions
    source = FixtureSource()
    one, two, three = [source.add_work(str(number), language='中文-普通话 國語', language_id='zh') for number in (11, 12, 21)]
    source.recent_pages[None] = Page([one], 'https://reader.example/recent')
    first, second = 'https://reader.example/search?q=rain', 'https://reader.example/search?q=rain&page=2'
    source.search_pages['rain', None] = Page([one, two], first, second)
    source.search_pages['rain', second] = Page([three], second, previous_url=first)
    app = MiniNotesApp(ReaderEngine(source, MemoryStore()))
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.open_search()
        app.main_query('#any-field', Input).value = 'rain'
        await pilot.press('enter')
        await settled(pilot)
        await pilot.press('down', 'n')
        await settled(pilot)
        assert app.engine.results_page_number == 2
        assert len(app.engine.results.items) == 1
        assert app.main_query('#results-list', ResultOptions).option_count == 1
        assert not app.main_query('#results-previous').disabled
        await pilot.press('p')
        await settled(pilot)
        assert app.engine.results_page_number == 1
        assert app.main_query('#results-list', ResultOptions).highlighted == 1
        await pilot.press('enter')
        await settled(pilot)
        assert app.engine.current.work.id == '12'
        app.focus_command('/results')
        await pilot.press('enter')
        assert app.main_query('#results-list', ResultOptions).highlighted == 1


@pytest.mark.asyncio
async def test_shortcuts_skip_and_input_focus_do_not_conflict():
    engine = FakeEngine()
    async def skip_work():
        engine.calls.append('skip_work')
    async def skip_series():
        engine.calls.append('skip_series')
    engine.skip_work, engine.skip_series = skip_work, skip_series
    engine.series = [object()]
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        await pilot.press('n')
        await settled(pilot)
        await pilot.press('p')
        await settled(pilot)
        await pilot.press(']')
        await settled(pilot)
        app.focus_command('/skip-series')
        await pilot.press('enter')
        await settled(pilot)
        assert all(name in engine.calls for name in ['next', 'previous', 'skip_work'])
        assert 'skip_series' not in engine.calls
        before = list(engine.calls)
        app.open_search()
        await pilot.press('n', 'p', ']')
        assert app.main_query('#any-field', Input).value == 'np]'
        assert engine.calls == before


@pytest.mark.asyncio
async def test_pending_next_shows_status_cancels_and_does_not_cancel_prefetch():
    from textual.widgets import Static
    engine = FakeEngine()
    preload = asyncio.Event()
    engine.prefetch_status = {'state': 'loading', 'ready': 0, 'target': 3, 'message': ''}
    async def prefetch():
        engine.calls.append('prefetch_started')
        await preload.wait()
        engine.prefetch_status = {'state': 'ready', 'ready': 3, 'target': 3, 'message': ''}
    engine.prefetch = prefetch
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        assert '后台正在准备后续内容' in str(app.main_query('#above-input', Static).content)
        await pilot.press('n')
        await settled(pilot)
        assert engine.calls.count('prefetch_started') == 1
        assert not app._prefetch_task.cancelled()
        engine.next_gate = asyncio.Event()
        await pilot.press('n')
        await settled(pilot)
        previous = engine.current.id
        assert app.busy and 'Esc' in str(app.main_query('#above-input', Static).content)
        await pilot.press('ctrl+g', 'ctrl+g', 'escape')
        await settled(pilot)
        assert not app.busy and engine.current.id == previous
        assert not app._prefetch_task.cancelled()
        preload.set()
        await settled(pilot)
        assert engine.prefetch_status['ready'] == 3


@pytest.mark.asyncio
async def test_search_draft_and_position_debounce_and_restore():
    from test_engine import FixtureSource, MemoryStore
    from mini_notes.engine import ReaderEngine
    source, store = FixtureSource(), MemoryStore()
    work = source.add_work('10', language='中文-普通话 國語', language_id='zh')
    source.recent_pages[None] = Page([work], 'https://reader.example/recent')
    engine = ReaderEngine(source, store)
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        submitted = engine.filters.query
        app.open_search()
        await pilot.press(*'draft search')
        await asyncio.sleep(1.15)
        await settled(pilot)
        assert store.state['search_draft']['query'] == 'draft search'
        assert store.state['search_draft']['language'] == 'zh'
        assert store.state['filters']['query'] == submitted
        await pilot.press('ctrl+c')
    reopened = MiniNotesApp(ReaderEngine(source, store))
    async with reopened.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        reopened.open_search()
        assert reopened.main_query('#any-field', Input).value == 'draft search'
        assert not list(reopened.screen_stack[0].query('#search-language'))
        assert reopened.engine.search_draft['language'] == 'zh'


@pytest.mark.asyncio
async def test_legacy_language_command_cannot_enable_other_languages():
    engine = FakeEngine()
    engine.filters.language = 'zh'
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        before = list(engine.calls)
        for value in ['/language', '/language en', '/language all', '/language yue']:
            app.focus_command(value)
            await pilot.press('enter')
            await settled(pilot)
            assert engine.filters.language == 'zh'
            assert engine.calls == before
            assert app.mode == 'reader' and not app.busy
            assert '仅支持中文' in app._local_notice


@pytest.mark.asyncio
async def test_restore_information_yields_to_idle_clock_and_does_not_hide_save_failure(monkeypatch):
    import mini_notes.app as app_module
    from textual.widgets import Static
    fixed_clock = '2040-01-02 03:04:05'
    # Keep real monotonic timers for toast expiry; only freeze this module's
    # displayed wall clock so an assertion cannot cross a second boundary.
    monkeypatch.setattr(app_module, 'time', SimpleNamespace(
        monotonic=app_module.time.monotonic, strftime=lambda _format: fixed_clock))
    engine = FakeEngine()
    engine.prefetch_status = {'state': 'ready', 'ready': 3, 'target': 3}
    app = MiniNotesApp(engine)
    app.TOAST_SECONDS = .15
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        engine.notice = '已从本地恢复上次位置'
        app.update_footer()
        assert '恢复' in str(app.main_query('#above-input', Static).content)
        for _ in range(4):
            await asyncio.sleep(.05)
            app.update_footer()
        assert str(app.main_query('#above-input', Static).content) == fixed_clock
        app.cancel_pending()
        assert '已取消' in str(app.main_query('#above-input', Static).content)
        await asyncio.sleep(.2)
        await pilot.pause()
        assert str(app.main_query('#above-input', Static).content) == fixed_clock
        engine.prefetch_status = {'state': 'idle', 'ready': 0, 'target': 3}
        app.reader.action_end()
        app.update_footer()
        assert str(app.main_query('#above-input', Static).content) == fixed_clock
        app.set_local_notice('本地进度暂未保存；退出时将重试', persistent=True)
        app.update_footer()
        await asyncio.sleep(.2)
        await pilot.pause()
        assert '未保存' in str(app.main_query('#above-input', Static).content)
        engine.storage_notice = '当前章节未保存：本地存储已达上限'
        app.set_local_notice('另一条会到期的信息')
        app.update_footer()
        await asyncio.sleep(.2)
        await pilot.pause()
        assert str(app.main_query('#above-input', Static).content) == engine.storage_notice


@pytest.mark.asyncio
@pytest.mark.parametrize('theme', ['dark', 'light'])
async def test_search_field_has_visible_frame_and_narrow_actions(theme):
    app = MiniNotesApp(FakeEngine(), theme_mode=theme)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        app.open_search()
        await settled(pilot)
        field = app.main_query('#any-field', Input)
        assert field.region.height == 3
        assert field.styles.border_top[0] != ''
        assert field.styles.padding.left == 1
        assert app.main_query('#search-buttons').region.bottom <= 9
        assert app.main_query('#main-footer').size.height == 6


def temporary_source_error(code='unavailable', *, retry_after=None, attempts=1):
    error = SourceError(code, 'HTTP 525: original test failure', retry_after=retry_after)
    error.retryable = True
    error.attempts = attempts
    return error


@pytest.mark.asyncio
async def test_transient_next_stays_on_body_and_recovers_with_bounded_retries():
    from textual.widgets import Static
    engine = FakeEngine()
    attempts = []
    third = asyncio.Event()
    async def next_attempt():
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise temporary_source_error()
        await third.wait()
        engine.current = chapter(2)
        engine.position = {}
        return engine.current
    engine.next = next_attempt
    app = MiniNotesApp(engine)
    app.RETRY_DELAYS = (.08, .08)
    app.RETRY_BUDGET = 2
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        original = app.reader.paragraphs
        app.navigate(1)
        await asyncio.sleep(.03)
        await pilot.pause()
        assert app.mode == 'reader' and app.reader.paragraphs == original
        assert '3' in str(app.main_query('#above-input', Static).content)
        await pilot.press('ctrl+g', 'ctrl+g')
        assert app.reader.paragraphs == original
        await asyncio.sleep(.2)
        third.set()
        await settled(pilot)
        assert attempts == [1, 2, 3]
        assert engine.current.id == '2' and app.mode == 'reader'


@pytest.mark.asyncio
async def test_retry_exhaustion_keeps_body_and_r_retries_without_clearing_anchor():
    from textual.widgets import Static
    engine = FakeEngine()
    attempts = []
    fail = True
    async def next_attempt():
        attempts.append(1)
        if fail:
            raise temporary_source_error()
        engine.current = chapter(2)
        return engine.current
    engine.next = next_attempt
    app = MiniNotesApp(engine)
    app.RETRY_DELAYS = (.01, .01)
    app.RETRY_BUDGET = 1
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.reader.move(8)
        anchor = app.reader.anchor()
        app.navigate(1)
        await asyncio.sleep(.12)
        await settled(pilot)
        assert len(attempts) == 3
        assert app.mode == 'reader' and app.reader.anchor() == anchor
        assert '/retry' in str(app.main_query('#above-input', Static).content)
        fail = False
        await pilot.press('r')
        await settled(pilot)
        assert engine.current.id == '2' and len(attempts) == 4


@pytest.mark.asyncio
async def test_long_retry_after_pauses_without_request_spam_or_fullscreen_error():
    from textual.widgets import Static
    engine = FakeEngine()
    engine.fail_next = temporary_source_error('rate_limited', retry_after=60)
    app = MiniNotesApp(engine)
    app.RETRY_BUDGET = .2
    app.RETRY_DELAYS = (.01, .01)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        app.navigate(1)
        await asyncio.sleep(.08)
        await settled(pilot)
        assert engine.calls.count('next') == 1
        assert app.mode == 'reader' and not app.busy
        assert '冷却' in str(app.main_query('#above-input', Static).content)
        await pilot.press('r')
        assert engine.calls.count('next') == 1


@pytest.mark.asyncio
async def test_escape_cancels_retry_wait_and_never_calls_again():
    engine = FakeEngine()
    engine.fail_next = temporary_source_error()
    app = MiniNotesApp(engine)
    app.RETRY_DELAYS = (.5, .5)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.navigate(1)
        await asyncio.sleep(.04)
        await pilot.press('escape')
        await asyncio.sleep(.55)
        assert engine.calls.count('next') == 1
        assert app.mode == 'reader' and not app.busy


@pytest.mark.asyncio
async def test_search_retry_escape_returns_to_reader_and_preserves_draft():
    engine = FakeEngine()
    calls = []
    async def search_fail(filters):
        calls.append(filters.query)
        raise temporary_source_error()
    engine.search = search_fail
    app = MiniNotesApp(engine)
    app.RETRY_DELAYS = (.5, .5)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        app.open_search()
        await pilot.press(*'rain day')
        await pilot.press('enter')
        await asyncio.sleep(.03)
        assert app.mode == 'search'
        await pilot.press('ctrl+g', 'ctrl+g', 'escape')
        assert app.mode == 'reader' and not app.busy
        assert app.main_query('#any-field', Input).value == 'rain day'
        assert app.focused is app.reader
        app.open_search()
        await pilot.pause()
        assert app.focused is app.main_query('#any-field')
        await asyncio.sleep(.55)
        assert calls == ['rain day']


@pytest.mark.asyncio
async def test_exhausted_background_worker_is_not_retried_three_more_times():
    engine = FakeEngine()
    failure = temporary_source_error(attempts=3)
    failure.retry_budget_exhausted = True
    engine.fail_next = failure
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.navigate(1)
        await settled(pilot)
        assert engine.calls.count('next') == 1
        assert app.mode == 'reader' and app._retry_pending


@pytest.mark.asyncio
async def test_retry_only_background_failure_does_not_repeat_successful_navigation():
    engine = FakeEngine()
    engine.prefetch_status = {'state': 'error', 'errors': ['network']}
    async def retry_background():
        engine.calls.append('retry_background')
        engine.prefetch_status = {'state': 'ready', 'errors': [], 'ready': 3, 'target': 3}
    engine.retry_prefetch = retry_background
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        await pilot.press('r')
        await settled(pilot)
        assert 'retry_background' in engine.calls
        assert 'next' not in engine.calls and engine.current.id == '1'


@pytest.mark.asyncio
async def test_production_network_error_class_recovers_without_error_panel():
    engine = FakeEngine()
    calls = []
    async def intermittent():
        calls.append(1)
        if len(calls) == 1:
            raise SourceError('network', '连接超时，当前位置保留。')
        engine.current = chapter(2)
        return engine.current
    engine.next = intermittent
    app = MiniNotesApp(engine)
    app.RETRY_DELAYS = (.01, .01)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.navigate(1)
        await asyncio.sleep(.08)
        await settled(pilot)
        assert len(calls) == 2 and engine.current.id == '2'
        assert app.mode == 'reader'


@pytest.mark.asyncio
async def test_operation_budget_cancels_hung_request_but_preserves_current_body():
    engine = FakeEngine()
    engine.next_gate = asyncio.Event()
    app = MiniNotesApp(engine)
    app.RETRY_BUDGET = .12
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        original = app.reader.paragraphs
        app.navigate(1)
        await asyncio.sleep(.2)
        await settled(pilot)
        assert not app.busy and app.mode == 'reader'
        assert app.reader.paragraphs == original and engine.calls.count('next') == 1
        assert app._retry_pending and app.last_error.retry_budget_exhausted


@pytest.mark.asyncio
async def test_cold_start_temporary_failure_is_inline_and_retryable():
    engine = FakeEngine()
    async def offline_start():
        raise SourceError('network', '原创网络测试')
    engine.start = offline_start
    app = MiniNotesApp(engine)
    app.RETRY_DELAYS = (.01, .01)
    async with app.run_test(size=(48, 15)) as pilot:
        await asyncio.sleep(.12)
        await settled(pilot)
        assert app.mode == 'reader' and not app.busy
        assert '暂未连接' in ''.join(app.reader.paragraphs)
        assert not app.main_query('#error-panel').display
        assert app._retry_pending


@pytest.mark.asyncio
async def test_cancel_old_operation_does_not_clear_new_request_status():
    engine = FakeEngine()
    engine.next_gate = asyncio.Event()
    gate = asyncio.Event()
    async def search_wait(filters):
        await gate.wait()
        return Page([], 'https://reader.example/search')
    engine.search = search_wait
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.navigate(1)
        await asyncio.sleep(.01)
        app.cancel_pending()
        app.open_search()
        app.action_submit_search()
        await asyncio.sleep(.05)
        assert app.busy and app._progress is not None and app.mode == 'search'
        gate.set()
        await settled(pilot)
        assert not app.busy


@pytest.mark.asyncio
async def test_cancel_pauses_promoted_background_retries_without_clearing_body():
    engine = FakeEngine()
    engine.next_gate = asyncio.Event()
    def pause_prefetch():
        engine.calls.append('pause_prefetch')
    engine.pause_prefetch = pause_prefetch
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        body = app.reader.paragraphs
        app.navigate(1)
        await asyncio.sleep(.01)
        await pilot.press('escape')
        assert engine.calls.count('pause_prefetch') == 1
        assert not app.busy and app.reader.paragraphs == body


@pytest.mark.asyncio
async def test_server_cooldown_does_not_disable_already_cached_previous_content():
    engine = FakeEngine()
    engine.fail_next = temporary_source_error('rate_limited', retry_after=60)
    app = MiniNotesApp(engine)
    app.RETRY_BUDGET = .2
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        app.navigate(1)
        await settled(pilot)
        assert app._recovery
        await pilot.press('p')
        await settled(pilot)
        assert 'previous' in engine.calls
        assert app.mode == 'reader' and engine.current.id == '1'


@pytest.mark.asyncio
@pytest.mark.parametrize('theme', ['light', 'dark'])
async def test_manual_theme_input_does_not_mix_default_background(theme, monkeypatch):
    monkeypatch.delenv('NO_COLOR', raising=False)
    from rich.color import Color
    app = MiniNotesApp(FakeEngine(), theme_mode=theme)
    async with app.run_test(size=(100, 24)) as pilot:
        await settled(pilot)
        app.open_search()
        await pilot.press(*'readable')
        await settled(pilot)
        expected_fg = Color.parse(app.terminal_palette.foreground)
        expected_bg = Color.parse(app.terminal_palette.background)
        for token in ('readable',):
            segments = [segment for strip in app.screen._compositor.render_strips() for segment in strip if token in segment.text]
            assert segments
            assert all(not segment.style.bgcolor.is_default and segment.style.color.get_truecolor() == expected_fg.get_truecolor() and segment.style.bgcolor.get_truecolor() == expected_bg.get_truecolor() for segment in segments)
        assert not list(app.screen_stack[0].query('#search-language'))


def test_safe_failure_reason_uses_status_or_allowlisted_kind_only():
    error = SourceError('network', 'https://user:password@proxy.invalid', failure_kind='read_timeout')
    assert MiniNotesApp.failure_reason(error) == '读取超时'
    assert MiniNotesApp.failure_reason(SourceError('unavailable', 'hidden', http_status=525)) == '525'
    assert 'password' not in MiniNotesApp.failure_reason(Exception('user:password'))


@pytest.mark.asyncio
async def test_v4_idle_clock_book_progress_and_loading_priority():
    import re
    from rich.cells import cell_len
    from textual.widgets import Static
    engine = FakeEngine()
    engine.prefetch_status = {'state': 'ready', 'ready': 0, 'current_work_ready': 0}
    app = MiniNotesApp(engine)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        engine.current.work.title = '这是需要按显示列截断的很长的书名' * 4
        engine.current.work.chapter_total = 12
        app.reader.move(12)
        app.update_footer()
        above = str(app.main_query('#above-input', Static).content)
        assert re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', above)
        footer = str(app.main_query('#footer-first', Static).content)
        assert '第1/12章' in footer and f'本章进度{app.reader.percent}%' in footer
        assert cell_len(footer) <= 46 and 'Workspace' not in footer
        app.open_search()
        app.update_footer()
        assert str(app.main_query('#footer-first', Static).content) == footer
        engine.next_gate = asyncio.Event()
        app.navigate(1)
        await settled(pilot)
        assert 'Esc取消' in str(app.main_query('#above-input', Static).content)
        await pilot.press('ctrl+g')
        assert '本章进度' not in str(app.screen.query_one('#mask-footer-first', Static).content)
        engine.next_gate.set()


@pytest.mark.asyncio
async def test_v4_menu_tab_completes_without_executing_and_marker_moves():
    from mini_notes.models import SeriesRef
    engine = FakeEngine()
    engine.series = [SeriesRef('7', 'https://archiveofourown.org/series/7', 'Sequence')]
    app = MiniNotesApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(pilot)
        await pilot.press('/', 's', 'k', 'i', 'p', '-', 'down', 'tab')
        assert app.main_query('#command', Input).value == '/skip-work'
        assert app.mode == 'reader' and not app.busy
        assert app.focused is app.main_query('#command')
        app.focus_command('/')
        await settled(pilot)
        assert not set(['/source', '/open', '/cache']) & set(app.command_matches)
        assert '/storage' in app.command_matches
        await pilot.press('down')
        menu = app.main_query('#command-menu')
        assert str(menu.get_option_at_index(menu.highlighted).prompt).startswith('> ')
        assert sum(str(menu.get_option_at_index(i).prompt).startswith('> ') for i in range(menu.option_count)) == 1
        await pilot.press('ctrl+g', 'ctrl+g')
        assert str(menu.get_option_at_index(menu.highlighted).prompt).startswith('> ')


@pytest.mark.asyncio
async def test_v4_empty_context_uses_inline_hints_and_no_results_opens_search():
    from textual.widgets import Static
    engine = FakeEngine()
    engine.restore_results = lambda: engine.results
    app = MiniNotesApp(engine)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        app.focus_command('/')
        await settled(pilot)
        assert '/skip-series' not in app.command_matches
        for command in ['/series', '/source', '/open https://archiveofourown.org/works/1', '/skip-series']:
            app.execute_command(command)
            assert app.mode == 'reader' and app._local_notice
        app.execute_command('/results')
        assert app.mode == 'search'
        assert '尚无' in str(app.main_query('#above-input', Static).content)
        engine.results = Page([], 'https://archiveofourown.org/works/search')
        app.execute_command('/results')
        assert app.mode == 'results'
        assert '没有匹配' in str(app.main_query('#results-summary', Static).content)


@pytest.mark.asyncio
async def test_v4_search_labels_and_checkbox_glyphs_are_unambiguous():
    from mini_notes.app import WARNING_LABELS, CATEGORY_LABELS
    from mini_notes.models import WARNING_OPTIONS, CATEGORY_OPTIONS
    assert set(WARNING_LABELS) == set(WARNING_OPTIONS)
    assert set(CATEGORY_LABELS) == set(CATEGORY_OPTIONS)
    app = MiniNotesApp(FakeEngine())
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        app.open_search()
        await settled(pilot)
        assert not list(app.screen_stack[0].query('#search-language'))
        assert str(app.main_query('#filter-help').content) == ''
        app.main_query('#search-tabs', Tabs).active = 'tab-warnings'
        await settled(pilot)
        choices = app.main_query('#warning-options', SearchSelection)
        assert '[ ]' in choices.render_line(0).text and '[x]' not in choices.render_line(0).text
        await pilot.press('space')
        assert '[x]' in choices.render_line(0).text
        assert '[ ]' in choices.render_line(1).text
        assert choices.selected == ['14']
        await pilot.press('ctrl+g', 'ctrl+g')
        assert '[x]' in choices.render_line(0).text


@pytest.mark.asyncio
async def test_v4_storage_uses_actual_info_safe_clear_and_persistent_save_failure():
    from textual.widgets import Static
    engine = FakeEngine()
    values = {'db_bytes': 8192, 'journal_bytes': 0, 'wal_bytes': 0, 'shm_bytes': 0,
              'managed_file_bytes': 8192, 'cache_payload_bytes': 1024,
              'cache_entries': 2, 'session_bytes': 4096, 'preferences_bytes': 128,
              'limits': {'cache_bytes': 20971520, 'cache_entries': 64,
                         'session_bytes': 4194304, 'preferences_bytes': 65536,
                         'database_bytes': 33554432}}
    engine.store = SimpleNamespace(storage_info=lambda: dict(values))
    engine.storage_notice = ''
    def clear():
        engine.calls.append('clear_prepared_cache')
        values.update(cache_payload_bytes=0, cache_entries=0)
        return {**values, 'cleared': True, 'paused': True}
    engine.clear_prepared_cache = clear
    engine.save = lambda: False
    app = MiniNotesApp(engine)
    app.TOAST_SECONDS = .05
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        app.reader.move(7)
        anchor = app.reader.anchor()
        app.execute_command('/storage')
        assert app.mode == 'storage'
        await settled(pilot)
        assert '20.00 MiB' in str(app.main_query('#storage-message', Static).content)
        assert '64' in str(app.main_query('#storage-message', Static).content)
        await pilot.click('#storage-clear')
        await settled(pilot)
        assert engine.calls.count('clear_prepared_cache') == 1
        assert app.reader.anchor() == anchor and engine.current is not None
        assert '0 / 64' in str(app.main_query('#storage-message', Static).content)
        engine.storage_notice = '当前章节未保存：本地存储已达上限'
        app.flush_save()
        await asyncio.sleep(.1)
        app.update_footer()
        assert '未保存' in str(app.main_query('#above-input', Static).content)
        await pilot.press('ctrl+g', 'ctrl+g')
        assert app.mode == 'storage'


@pytest.mark.asyncio
@pytest.mark.parametrize('theme', ['light', 'dark', 'auto'])
async def test_v4_command_menu_unfocused_highlight_has_readable_color_pair(theme, monkeypatch):
    monkeypatch.delenv('NO_COLOR', raising=False)
    app = MiniNotesApp(FakeEngine(), theme_mode=theme)
    async with app.run_test(size=(100, 24)) as pilot:
        await settled(pilot)
        await pilot.press('/', 'down', 'down')
        menu = app.main_query('#command-menu')
        assert app.focused is app.main_query('#command') and not menu.has_focus
        selected = [segment for strip in menu.render_lines(menu.region.at_offset((0, 0))) for segment in strip if '> /prev' in segment.text]
        # The component contract matters even if an SVG/font renderer drops ink.
        style = menu.get_component_rich_style('option-list--option-highlighted')
        from rich.color import Color
        assert style.color.get_truecolor() == Color.parse(app.terminal_palette.block_foreground).get_truecolor()
        assert not style.bgcolor.is_default
        assert style.bgcolor.get_truecolor() == Color.parse(app.terminal_palette.block).get_truecolor()
        assert selected


@pytest.mark.asyncio
async def test_chinese_only_search_ignores_legacy_language_and_has_no_selector():
    from textual.widgets import Select
    engine = FakeEngine()
    engine.filters = SearchFilters(language='en')
    engine.search_draft = {'query': 'rain', 'warnings': ['16'], 'categories': ['21'], 'language': 'en'}
    app = MiniNotesApp(engine)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        app.open_search()
        assert not list(app.screen_stack[0].query('#search-language'))
        assert not list(app.screen_stack[0].query(Select))
        assert app.main_query('#any-field', Input).value == 'rain'
        assert engine.search_draft['language'] == 'zh'
        app.focus_command('/')
        await settled(pilot)
        assert '/language' not in app.command_matches
        app.open_search()
        await pilot.press('enter')
        await settled(pilot)
        assert engine.filters.language == 'zh'
        assert engine.filters.query == 'rain'
        assert engine.filters.warnings == ['16'] and engine.filters.categories == ['21']
        app.open_search()
        await pilot.press('ctrl+g', 'ctrl+g')
        assert app.focused is app.main_query('#any-field')
        assert not list(app.screen_stack[0].query('#search-language'))


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_action', ['escape', 'button', 'command'])
@pytest.mark.parametrize('late_completion', [False, True])
async def test_exit_pending_search_cancels_and_late_results_cannot_take_over(exit_action, late_completion):
    from test_engine import FixtureSource, MemoryStore
    from mini_notes.engine import ReaderEngine
    from textual.widgets import Static
    source, store = FixtureSource(), MemoryStore()
    work = source.add_work('10', language='中文-普通话 國語', language_id='zh')
    source.recent_pages[None] = Page([work], 'https://reader.example/recent')
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = 0
    async def waiting_search(**kwargs):
        nonlocal active
        active += 1
        entered.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                if not late_completion:
                    raise
                await release.wait()
            return Page([work], 'https://reader.example/search?new')
        finally:
            active -= 1
    source.search = waiting_search
    engine = ReaderEngine(source, store)
    app = MiniNotesApp(engine)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        before = engine.current
        app.reader.move(1)
        anchor = app.reader.anchor()
        old_page = Page([], 'https://reader.example/search?old')
        engine.results = old_page
        old_query = engine.filters.query
        app.open_search()
        await pilot.press(*'unfinished query', 'enter')
        await asyncio.wait_for(entered.wait(), .5)
        await settled(pilot)
        assert app.busy and active == 1
        if exit_action == 'escape':
            await pilot.press('escape')
        elif exit_action == 'button':
            await pilot.click('#search-back')
        else:
            app.focus_command('/title')
            await pilot.press('enter')
        await asyncio.wait_for(cancelled.wait(), .5)
        assert app.mode == 'reader' and app.focused is app.reader and not app.busy
        assert app._progress is None and app._retry_pending is False
        assert engine.current == before and app.reader.anchor() == anchor
        assert '已取消搜索' in str(app.main_query('#above-input', Static).content)
        if not late_completion:
            assert active == 0
        await pilot.press('ctrl+g')
        release.set()
        await settled(pilot)
        assert active == 0
        await pilot.press('ctrl+g')
        assert app.mode == 'reader' and app.focused is app.reader
        assert engine.current == before and engine.filters.query == old_query
        assert engine.results.url == old_page.url
        assert app.reader.anchor() == anchor and not app.busy
        assert not app.main_query('#results-panel').display
        app.open_search()
        assert app.main_query('#any-field', Input).value == 'unfinished query'
        assert engine.search_draft['query'] == 'unfinished query'
        await pilot.press('ctrl+c')


@pytest.mark.asyncio
async def test_removed_series_skip_has_no_ui_or_keyboard_action():
    from rich.cells import cell_len
    from mini_notes.models import SeriesRef
    from textual.widgets import Static
    engine = FakeEngine()
    engine.series = [SeriesRef('1', 'https://archiveofourown.org/series/1', '原创系列')]
    async def skip_series():
        engine.calls.append('skip_series')
    engine.skip_series = skip_series
    app = MiniNotesApp(engine)
    async with app.run_test(size=(48, 15)) as pilot:
        await settled(pilot)
        hints = str(app.main_query('#footer-second', Static).content)
        assert 'n/p 前后章' in hints and 'Ctrl+G 秒切工作' in hints
        assert ']跳作' in hints and '系列' not in hints and cell_len(hints) <= 46
        await pilot.press('}')
        await settled(pilot)
        assert engine.calls.count('skip_series') == 0
        app.open_search()
        await pilot.press('n', 'p', ']', '}')
        assert app.main_query('#any-field', Input).value == 'np]}'
        assert engine.calls.count('skip_series') == 0
        await pilot.press('ctrl+g', '}','ctrl+g')
        assert engine.calls.count('skip_series') == 0
        await pilot.press('escape')
        engine.series = []
        app.update_footer()
        assert '}跳系列' not in str(app.main_query('#footer-second', Static).content)
        await pilot.press('}')
        assert engine.calls.count('skip_series') == 0
        app.execute_command('/help')
        text = str(app.main_query('#info-message', Static).content)
        assert '] 跳过本作' in text
        assert '跳过系列' not in text and '/skip-series' not in text
        assert '/skip-series' not in dict(app.available_commands())
