"""Self-authored pages exercise confirmation through real source, engine and TUI."""

import asyncio

import httpx
import pytest
from textual.widgets import Button, Static

from mini_notes.app import MiniNotesApp
from mini_notes.engine import ReaderEngine
from mini_notes.models import Page, WorkSummary
from mini_notes.source import AO3Source, SourceError
from test_engine import MemoryStore

BASE = "https://reader.example"


def warning(path, href=None):
    return (f'<div id="main"><p class="caution notice">This work could have adult '
            f'content. Do you wish to proceed?</p><ul class="actions"><li>'
            f'<a href="{href or path + "?view_adult=true"}">Proceed</a></li></ul>'
            '<ol class="work index"><li class="work blurb">Test metadata</li></ol></div>')


def body(work="101", number=1):
    previous = f'<li class="chapter previous"><a href="/works/{work}/chapters/{number - 1}">Previous Chapter</a></li>' if number > 1 else ''
    following = f'<li class="chapter next"><a href="/works/{work}/chapters/{number + 1}">Next Chapter</a></li>' if number < 3 else ''
    return f'''<div id="main"><ul class="work navigation actions">{previous}{following}</ul>
    <div id="workskin"><h2 class="title">自拟确认测试</h2><dl class="work meta">
    <dd class="rating tags"><a>Not Rated</a></dd><dd class="language">中文-普通话 國語</dd>
    <dd class="chapters">3/3</dd></dl><div id="chapters"><div class="chapter">
    <div class="preface"><h3 class="title">Chapter {number}</h3></div>
    <div class="userstuff module" role="article"><p>自拟正文 {work}/{number}，窗外在下雨。</p></div>
    </div></div></div><select id="selected_id">
    {''.join(f'<option value="{n}">{n}</option>' for n in range(1, 4))}</select></div>'''


def source_for(handler):
    return AO3Source(BASE, transport=httpx.MockTransport(handler), min_interval=0, max_retries=0)


@pytest.mark.asyncio
async def test_confirmation_is_explicit_scoped_to_work_and_survives_chapter_redirects():
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if request.url.params.get("view_adult") != "true":
            return httpx.Response(200, text=warning(path))
        if "/chapters/" not in path:
            return httpx.Response(302, headers={"Location": path + "/chapters/1", "Set-Cookie": "view_adult=true; Path=/"})
        return httpx.Response(200, text=body(number=int(path.rsplit('/', 1)[1])), headers={"Set-Cookie": "view_adult=true; Path=/"})

    async with source_for(handler) as source:
        with pytest.raises(SourceError) as caught:
            await source.get_work("101")
        assert caught.value.code == "adult_confirmation"
        assert caught.value.confirmation_url == BASE + "/works/101"
        assert len(requests) == 1 and "view_adult" not in requests[0].url.params
        source.confirm_content_warning(caught.value.confirmation_url)
        detail = await source.get_work("101")
        assert detail.chapter.id == "1"
        second = await source.get_chapter(detail.chapter.next_url)
        assert second.id == "2" and second.paragraphs == ["自拟正文 101/2，窗外在下雨。"]
        assert "view_adult" not in second.url
        assert all(r.url.params.get("view_adult") == "true" for r in requests[1:])
        with pytest.raises(SourceError, match="内容"):
            await source.get_work("202")
        assert "view_adult" not in requests[-1].url.params
        assert "view_adult" not in requests[-1].headers.get("cookie", "")
    # A new reader session does not silently inherit consent.
    async with source_for(handler) as source:
        with pytest.raises(SourceError):
            await source.get_work("101")
        assert "view_adult" not in requests[-1].url.params


@pytest.mark.asyncio
@pytest.mark.parametrize("href", ["https://other.example/works/101?view_adult=true", "/works/202?view_adult=true", "/users/login?view_adult=true", "/works/101?other=view_adult=true", "https://[broken]/works/101?view_adult=true"])
async def test_untrusted_proceed_link_cannot_enable_confirmation(href):
    async with source_for(lambda r: httpx.Response(200, text=warning(r.url.path, href))) as source:
        with pytest.raises(SourceError) as caught:
            await source.get_work("101")
        assert caught.value.code == "adult_confirmation"
        assert caught.value.confirmation_url is None
        with pytest.raises(SourceError):
            source.confirm_content_warning(BASE + "/works/101")


@pytest.mark.asyncio
async def test_content_css_class_in_valid_body_is_not_a_confirmation_page():
    page = body().replace('<p>自拟正文', '<p class="adult">自拟正文')
    async with source_for(lambda r: httpx.Response(200, text=page)) as source:
        assert (await source.get_work("101")).chapter.paragraphs


@pytest.mark.asyncio
async def test_login_form_takes_precedence_over_content_notice():
    page = warning('/works/101').replace('</div>', '<form action="/users/login"><input type="password"></form></div>')
    async with source_for(lambda r: httpx.Response(200, text=page)) as source:
        with pytest.raises(SourceError) as caught:
            await source.get_work("101")
        assert caught.value.code == "login_required"
        assert caught.value.confirmation_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["cold", "next", "result"])
async def test_terminal_confirmation_retries_original_navigation_and_replenishes(entry):
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path in {"/works", "/works/search"}:
            return httpx.Response(200, text='<div id="main"><ol class="work index"></ol></div>')
        work = path.split('/')[2]
        number = int(path.rsplit('/', 1)[1]) if '/chapters/' in path else 1
        needs_confirmation = work == "101" and not (entry == "next" and number == 1)
        if needs_confirmation and request.url.params.get('view_adult') != 'true':
            return httpx.Response(200, text=warning(path))
        if '/chapters/' not in path:
            return httpx.Response(302, headers={"Location": path + "/chapters/1"})
        return httpx.Response(200, text=body(work, number))

    source = source_for(handler)
    reader = ReaderEngine(source, MemoryStore())

    async def start():
        return await reader.open_url(BASE + ("/works/202/chapters/1" if entry == "result" else "/works/101/chapters/1"))

    reader.start = start
    app = MiniNotesApp(reader)
    try:
        async with app.run_test(size=(100, 26)) as pilot:
            await app._operation_task
            await pilot.pause()
            if entry != "cold":
                if app._prefetch_task:
                    await asyncio.wait_for(app._prefetch_task, 3)
                previous = reader.current
                if entry == "next":
                    app.navigate(1)
                else:
                    reader.results = Page([WorkSummary(id="101", url=BASE + "/works/101", title="自拟目标")], BASE + "/works/search")
                    app.queue_operation("正在打开作品…", lambda: reader.open_result(0), "reader")
                await app._operation_task
                assert reader.current == previous
            assert app.mode == "error"
            assert app.main_query('#error-confirm', Button).display
            assert app.focused.id == 'error-back'
            assert '可能包含成人内容' in str(app.main_query('#error-message', Static).content)
            assert not any('view_adult' in r.url.params for r in requests)
            # Returning to the reader and retrying alone never grants consent.
            await pilot.press('escape')
            app.action_retry()
            await app._operation_task
            await pilot.pause()
            assert app.mode == "error"
            assert not any('view_adult' in r.url.params for r in requests)
            await pilot.click('#error-confirm')
            await app._operation_task
            await pilot.pause()
            assert app.mode == 'reader' and app.last_error is None
            assert reader.current.work.id == "101"
            assert reader.current.id == ('2' if entry == 'next' else '1')
            if entry == 'result':
                assert reader._flow.active_kind == 'search'
                assert reader.results.items[0].id == '101'
            if app._prefetch_task:
                await asyncio.wait_for(app._prefetch_task, 3)
            assert reader.prefetch_status['current_work_ready'] >= 1
            before = len(requests)
            app.navigate(1)
            await app._operation_task
            assert reader.current.id == ('3' if entry == 'next' else '2')
            assert len(requests) == before  # Prepared successor is consumed locally.
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_confirmation_followed_by_network_failure_restores_reader_and_can_retry():
    fail = True

    def handler(request):
        if request.url.path in {"/works", "/works/search"}:
            return httpx.Response(200, text='<div id="main"><ol class="work index"></ol></div>')
        if request.url.params.get('view_adult') != 'true':
            return httpx.Response(200, text=warning(request.url.path))
        if fail:
            raise httpx.ConnectTimeout('Self-authored timeout')
        return httpx.Response(200, text=body())

    reader = ReaderEngine(source_for(handler), MemoryStore())

    async def start():
        return await reader.open_url(BASE + '/works/101/chapters/1')

    reader.start = start
    app = MiniNotesApp(reader)
    app.RETRY_ATTEMPTS = 1
    try:
        async with app.run_test(size=(100, 26)) as pilot:
            await app._operation_task
            await pilot.pause()
            await pilot.click('#error-confirm')
            await app._operation_task
            await pilot.pause()
            assert app.mode == 'reader'
            assert reader.current is None and app.last_error.code == 'network'
            assert not app.main_query('#error-panel').display
            fail = False
            app.action_retry()
            await app._operation_task
            assert reader.current.id == '1'
            assert app.last_error is None
    finally:
        await reader.close()
