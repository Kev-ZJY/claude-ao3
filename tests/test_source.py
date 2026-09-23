"""Hand-authored AO3-shaped fixtures; no third-party work text is stored here."""

import asyncio
import importlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

BASE = "https://reader.example"
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text()


def latest_fixture():
    """The official /works sample has no pagination, unlike Work Search."""
    return re.sub(
        r'<ol class="pagination actions">.*?</ol>',
        "",
        fixture("ao3_listing.html"),
        flags=re.S,
    )


def make_source(handler, **kwargs):
    module = importlib.import_module("mini_notes.source")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return module.AO3Source(BASE, client=client, min_interval=0, max_retries=1, **kwargs)


def test_search_sends_and_filters_preserves_unicode_and_strips_unapproved_consent():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=fixture("ao3_listing.html"))

    async def run():
        async with make_source(handler) as source:
            page = await source.search(
                "雨 AND light", warnings=["16", "18"], categories=["21", "22"]
            )
            assert [x.id for x in page.items] == ["101", "102"]
            assert page.items[0].title == "雨后 · Rain & Light"
            assert page.items[0].authors == ["Writer"]
            assert page.items[0].chapter_count == 2
            assert page.items[0].chapter_total == 3
            assert "view_adult" not in parse_qs(urlsplit(page.next_url).query)
            assert parse_qs(urlsplit(page.next_url).query)["page"] == ["2"]

    asyncio.run(run())
    query = parse_qs(requests[0].url.query.decode())
    assert query["work_search[query]"] == ["雨 AND light"]
    assert query["work_search[archive_warning_ids][]"] == ["16", "18"]
    assert query["work_search[category_ids][]"] == ["21", "22"]


def test_unfiltered_recent_bootstraps_latest_then_transitions_to_search_page_one():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        html = latest_fixture() if request.url.path == "/works" else fixture("ao3_listing.html")
        return httpx.Response(200, text=html)

    async def run():
        async with make_source(handler) as source:
            page = await source.recent()
            assert page.url == BASE + "/works"
            assert [work.id for work in page.items] == ["101", "102"]
            assert page.items[0].title == "雨后 · Rain & Light"
            assert page.items[0].rating == "General Audiences"
            assert page.items[0].warnings == ["No Archive Warnings Apply"]
            assert page.items[0].categories == ["Gen"]
            # This is an explicit transition to searchable recent works, NOT
            # an invented second page of the finite /works sample.
            bridge = parse_qs(urlsplit(page.next_url).query, keep_blank_values=True)
            assert urlsplit(page.next_url).path == "/works/search"
            assert bridge["page"] == ["1"]
            assert bridge["work_search[query]"] == [""]
            following = await source.recent(page_url=page.next_url)
            await source.recent(page_url=following.next_url)

    asyncio.run(run())
    assert seen[0] == BASE + "/works"
    transition = parse_qs(urlsplit(seen[1]).query)
    assert transition["work_search[sort_column]"] == ["revised_at"]
    assert transition["work_search[sort_direction]"] == ["desc"]
    assert transition["page"] == ["1"]
    assert parse_qs(urlsplit(seen[2]).query)["page"] == ["2"]


@pytest.mark.parametrize(
    "filters,parameter,value",
    [
        ({"warnings": ["16"]}, "work_search[archive_warning_ids][]", "16"),
        ({"categories": ["21"]}, "work_search[category_ids][]", "21"),
        ({"rating": "10"}, "work_search[rating_ids]", "10"),
        ({"language": "zh", "rating": "10"}, "work_search[language_id]", "zh"),
        ({"language": "zh", "warnings": ["16"]}, "work_search[language_id]", "zh"),
        ({"language": "zh", "categories": ["21"]}, "work_search[language_id]", "zh"),
    ],
)
def test_any_hard_filter_keeps_recent_on_filtered_search(filters, parameter, value):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=fixture("ao3_listing.html"))

    async def run():
        async with make_source(handler) as source:
            await source.recent(**filters)

    asyncio.run(run())
    assert len(seen) == 1
    assert seen[0].url.path == "/works/search"
    query = parse_qs(seen[0].url.query.decode())
    assert query[parameter] == [value]
    assert query["work_search[sort_column]"] == ["revised_at"]


def test_language_only_recent_filters_finite_index_then_bridges_with_language():
    seen = []
    latest = latest_fixture().replace(
        "</li></ol>",
        '</li><li class="work blurb"><h4 class="heading"><a href="/works/103">'
        'English fixture</a></h4><dl class="stats"><dd class="language">English</dd></dl>'
        "</li></ol>",
    )
    search = fixture("ao3_listing.html").replace(
        "work_search%5Bquery%5D=rain", "work_search%5Blanguage_id%5D=zh"
    )

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=latest if request.url.path == "/works" else search)

    async def run():
        async with make_source(handler) as source:
            page = await source.recent(language="zh")
            assert page.url == BASE + "/works"
            # English and the row with missing language metadata are both excluded.
            assert [work.id for work in page.items] == ["101"]
            bridge = parse_qs(urlsplit(page.next_url).query)
            assert bridge["work_search[language_id]"] == ["zh"]
            assert bridge["page"] == ["1"]
            following = await source.recent(page_url=page.next_url, language="zh")
            await source.recent(page_url=following.next_url, language="zh")

    asyncio.run(run())
    assert [request.url.path for request in seen] == ["/works", "/works/search", "/works/search"]
    assert [request.url.params["work_search[language_id]"] for request in seen[1:]] == ["zh", "zh"]
    assert [request.url.params["page"] for request in seen[1:]] == ["1", "2"]


def test_language_recent_with_zero_sample_matches_keeps_real_search_bridge():
    seen = []

    def handler(request):
        seen.append(request)
        html = (
            latest_fixture().replace("中文-普通话 國語", "English")
            if request.url.path == "/works"
            else fixture("ao3_listing.html")
        )
        return httpx.Response(200, text=html)

    async def run():
        async with make_source(handler) as source:
            sample = await source.recent(language="zh")
            assert sample.items == []
            assert sample.next_url is not None
            assert parse_qs(urlsplit(sample.next_url).query)["work_search[language_id]"] == ["zh"]
            actual_search = await source.recent(page_url=sample.next_url, language="zh")
            assert actual_search.items[0].id == "101"

    asyncio.run(run())
    assert len(seen) == 2


@pytest.mark.parametrize("status,code", [(200, "parse_error"), (403, "forbidden")])
def test_unrecognized_or_forbidden_latest_does_not_silently_fallback(status, code):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, text="<html><h1>Unrecognized source</h1></html>")

    async def run():
        async with make_source(handler) as source:
            with pytest.raises(Exception) as error:
                await source.recent()
            assert error.value.code == code

    asyncio.run(run())
    assert len(seen) == 1


def test_chapter_parses_only_body_preserves_paragraphs_and_navigation():
    async def run():
        async with make_source(
            lambda r: httpx.Response(200, text=fixture("ao3_chapter.html"))
        ) as source:
            chapter = await source.get_chapter("/works/101/chapters/1001")
            assert chapter.id == "1001"
            assert chapter.work.id == "101"
            assert chapter.work.title == "雨后 · Rain & Light"
            assert chapter.paragraphs[:3] == [
                "这是原创测试段落。Emphasis stays.",
                "Line one.\nLine two.",
                "A nested quotation.",
            ]
            body = "\n".join(chapter.paragraphs)
            assert "前言" not in body and "后记" not in body and "摘要" not in body
            assert "Chapter Text" not in body and "unsafe" not in body and "\x1b" not in body
            assert chapter.previous_url == BASE + "/works/101/chapters/1000"
            assert chapter.next_url == BASE + "/works/101/chapters/1002"
            assert [s.id for s in chapter.series] == ["55", "66"]

    asyncio.run(run())


def test_work_follows_same_origin_chapter_redirect_and_reads_directory():
    def handler(request):
        if request.url.path == "/works/101":
            return httpx.Response(302, headers={"location": "/works/101/chapters/1001"})
        return httpx.Response(200, text=fixture("ao3_chapter.html"))

    async def run():
        async with make_source(handler) as source:
            work = await source.get_work("101")
            assert work.work.id == "101" and work.chapter.id == "1001"
            assert [c.id for c in work.chapters] == ["1000", "1001", "1002"]

    asyncio.run(run())


def test_series_preserves_visible_source_order_without_id_arithmetic():
    async def run():
        async with make_source(
            lambda r: httpx.Response(200, text=fixture("ao3_series.html"))
        ) as source:
            series = await source.get_series("/series/55")
            assert series.id == "55"
            assert series.title == "旅程系列"
            assert [w.id for w in series.works] == ["205", "101"]

    asyncio.run(run())


def test_single_work_body_has_no_article_role_in_real_ao3_layout():
    async def run():
        async with make_source(
            lambda r: httpx.Response(200, text=fixture("ao3_single_work.html"))
        ) as source:
            detail = await source.get_work("101")
            assert detail.chapter.paragraphs == ["首段原创内容。", "第二段原创内容。"]
            assert detail.chapter.next_url is None
            assert detail.series[0].position == 1
            assert detail.work.rating == "General Audiences"
            assert detail.work.warnings == ["No Archive Warnings Apply"]
            assert detail.work.categories == ["Gen"]

    asyncio.run(run())


def test_work_metadata_outside_workskin_is_still_available_for_hard_filters():
    html = (
        fixture("ao3_single_work.html")
        .replace('<div id="workskin"><dl', "<dl")
        .replace(
            '</dl></dd></dl><div class="preface',
            '</dl></dd></dl><div id="workskin"><div class="preface',
        )
    )

    async def run():
        async with make_source(lambda r: httpx.Response(200, text=html)) as source:
            detail = await source.get_work("101")
            assert detail.work.rating == "General Audiences"
            assert detail.work.warnings == ["No Archive Warnings Apply"]
            assert detail.work.categories == ["Gen"]
            assert detail.work.chapter_total == 1

    asyncio.run(run())


def test_mixed_body_blocks_do_not_drop_text_outside_paragraph_tags():
    html = fixture("ao3_single_work.html").replace(
        "<p>首段原创内容。</p><p>第二段原创内容。</p>",
        "开头纯文本<div>块元素正文</div><p>中段</p>结尾纯文本",
    )

    async def run():
        async with make_source(lambda r: httpx.Response(200, text=html)) as source:
            detail = await source.get_work("101")
            assert detail.chapter.paragraphs == ["开头纯文本", "块元素正文", "中段", "结尾纯文本"]

    asyncio.run(run())


def test_advertised_unsafe_next_page_is_an_error_not_search_exhaustion():
    html = fixture("ao3_listing.html").replace(
        "/works/search?page=2&amp;work_search%5Bquery%5D=rain&amp;view_adult=true",
        "https://other.example/works/search?page=2",
    )

    async def run():
        async with make_source(lambda r: httpx.Response(200, text=html)) as source:
            with pytest.raises(Exception) as error:
                await source.search()
            assert error.value.code == "invalid_url"

    asyncio.run(run())


def test_official_adult_prompt_with_work_blurb_is_not_a_readable_work():
    page = '<div id="main"><p class="caution notice">Adult Content</p><ul class="actions"><li><a href="/works/101?view_adult=true">Proceed</a></li></ul><ol class="work index"><li class="work blurb">Metadata only</li></ol></div>'

    async def run():
        async with make_source(lambda r: httpx.Response(200, text=page)) as source:
            with pytest.raises(Exception) as error:
                await source.get_work("101")
            assert error.value.code == "adult_confirmation"

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/works/1",
        "//evil.example/works/1",
        "javascript:alert(1)",
        "https://user:pass@reader.example/works/1",
        "/users/login",
        "/works/1?view_adult=true",
    ],
)
def test_external_and_unapproved_urls_never_reach_transport(url):
    seen = []

    async def run():
        async with make_source(lambda r: seen.append(r) or httpx.Response(200, text="")) as source:
            with pytest.raises(Exception) as error:
                await source.get_work(url)
            assert error.value.code in {"invalid_url", "adult_confirmation", "login_required"}

    asyncio.run(run())
    assert not seen


@pytest.mark.parametrize(
    "status,html,code",
    [
        (401, "Authentication required", "login_required"),
        (403, "Forbidden", "forbidden"),
        (429, "Limited", "rate_limited"),
        (404, "Not found", "not_found"),
        (
            200,
            '<div id="main"><div class="adult"><h2>Adult content</h2><a href="?view_adult=true">Proceed</a></div></div>',
            "adult_confirmation",
        ),
        (
            200,
            '<div id="main"><h2>Log in</h2><form action="/users/login"><input type="password"></form></div>',
            "login_required",
        ),
        (200, "<html><h1>Unexpected page</h1></html>", "parse_error"),
    ],
)
def test_failure_pages_are_typed_not_empty_results(status, html, code):
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(
            status, text=html, headers={"Retry-After": "60"} if status == 429 else {}
        )

    async def run():
        async with make_source(handler) as source:
            with pytest.raises(Exception) as error:
                await source.get_work("101")
            assert error.value.code == code
            if status == 429:
                assert error.value.retry_after == 60

    asyncio.run(run())
    assert len(calls) == 1


def test_cross_origin_redirect_is_stopped_before_second_request():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/works/101"})

    async def run():
        async with make_source(handler) as source:
            with pytest.raises(Exception) as error:
                await source.get_work("101")
            assert error.value.code == "invalid_url"

    asyncio.run(run())
    assert len(seen) == 1


def test_login_redirect_is_typed_without_submitting_or_opening_login():
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(302, headers={"location": "/users/login?restricted=true"})

    async def run():
        async with make_source(handler) as source:
            with pytest.raises(Exception) as error:
                await source.get_work("101")
            assert error.value.code == "login_required"

    asyncio.run(run())
    assert len(seen) == 1


def test_site_header_login_form_does_not_block_public_reading():
    html = fixture("ao3_single_work.html").replace(
        "<body>",
        '<body><div id="header"><form action="/users/login"><input type="password"></form></div>',
    )

    async def run():
        async with make_source(lambda r: httpx.Response(200, text=html)) as source:
            assert (await source.get_work("101")).chapter.paragraphs

    asyncio.run(run())


def test_rate_limit_stops_followup_requests_until_retry_after():
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(429, headers={"Retry-After": "60"})

    async def run():
        async with make_source(handler) as source:
            for _ in range(2):
                with pytest.raises(Exception) as error:
                    await source.recent()
                assert error.value.code == "rate_limited"

    asyncio.run(run())
    assert len(seen) == 1


def test_source_passes_optional_rating_language_and_never_rewrites_query_operators():
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(200, text=fixture("ao3_listing.html"))

    async def run():
        async with make_source(handler) as source:
            await source.search('"Rain" OR 晴天', rating="10", language="zh")

    asyncio.run(run())
    query = parse_qs(seen[0].url.query.decode())
    assert query["work_search[rating_ids]"] == ["10"]
    assert query["work_search[language_id]"] == ["zh"]
    assert query["work_search[query]"] == ['"Rain" OR 晴天']


def test_empty_search_is_valid_only_with_recognized_results_container():
    async def run():
        async with make_source(
            lambda r: httpx.Response(
                200,
                text='<div id="main"><h2 class="heading">0 Works</h2><ol class="work index group"></ol></div>',
            )
        ) as source:
            page = await source.search("none")
            assert page.items == [] and page.next_url is None

    asyncio.run(run())


def test_timeout_is_retried_once_then_typed_network_error():
    seen = []

    def handler(r):
        seen.append(r)
        raise httpx.ReadTimeout("test timeout", request=r)

    async def run():
        async with make_source(handler, retry_delay=0) as source:
            with pytest.raises(Exception) as error:
                await source.recent()
            assert error.value.code == "network"

    asyncio.run(run())
    assert len(seen) == 2


def test_concurrent_identical_requests_are_cached_and_client_closed():
    seen = []

    async def handler(r):
        seen.append(r)
        await asyncio.sleep(0.01)
        return httpx.Response(200, text=fixture("ao3_listing.html"))

    async def run():
        source = make_source(handler)
        first, second = await asyncio.gather(source.recent(), source.recent())
        assert first.items[0].id == second.items[0].id == "101"
        await source.aclose()
        assert source._client.is_closed

    asyncio.run(run())
    assert len(seen) == 1


def test_retry_after_parse_failure_fetches_again_instead_of_replaying_bad_cache():
    seen = []

    def handler(r):
        seen.append(r)
        html = (
            "<html>Temporary incomplete response</html>"
            if len(seen) == 1
            else fixture("ao3_single_work.html")
        )
        return httpx.Response(200, text=html)

    async def run():
        async with make_source(handler) as source:
            with pytest.raises(Exception) as error:
                await source.get_work("101")
            assert error.value.code == "parse_error"
            detail = await source.get_work("101")
            assert detail.chapter.paragraphs == ["首段原创内容。", "第二段原创内容。"]

    asyncio.run(run())
    assert len(seen) == 2


@pytest.mark.parametrize("status,html", [(404, "missing"), (200, "<html>partial</html>")])
def test_valid_failed_target_is_available_for_foreground_retry(status, html):
    async def run():
        async with make_source(lambda r: httpx.Response(status, text=html)) as source:
            with pytest.raises(Exception) as error:
                await source.get_chapter("/works/101/chapters/1002")
            assert error.value.url == BASE + "/works/101/chapters/1002"

    asyncio.run(run())


def test_invalid_target_is_not_attached_to_user_visible_error():
    async def run():
        async with make_source(lambda r: httpx.Response(200, text="")) as source:
            with pytest.raises(Exception) as error:
                await source.get_work("https://user:secret@evil.example/works/1")
            assert error.value.url is None

    asyncio.run(run())


def test_foreground_overtakes_waiting_background_without_interrupting_inflight():
    seen = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            query = request.url.params.get("work_search[query]")
            seen.append(query)
            if query == "running":
                entered.set()
                await release.wait()
            return httpx.Response(200, text=fixture("ao3_listing.html"))

        async with make_source(handler) as source:

            async def read(query, priority):
                async with source.request_context(priority=priority):
                    return await source.search(query)

            running = asyncio.create_task(read("running", "background"))
            await asyncio.wait_for(entered.wait(), 1)
            background = asyncio.create_task(read("waiting", "background"))
            await asyncio.sleep(0)
            foreground = asyncio.create_task(read("foreground", "foreground"))
            await asyncio.sleep(0)
            assert seen == ["running"]
            release.set()
            await asyncio.gather(running, background, foreground)
            assert source.health["requests"] == 3

    asyncio.run(run())
    assert seen == ["running", "foreground", "waiting"]


def test_promoted_or_cancelled_waiter_does_not_duplicate_or_poison_requests():
    seen = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            query = request.url.params.get("work_search[query]")
            seen.append(query)
            if query == "running":
                entered.set()
                await release.wait()
            return httpx.Response(200, text=fixture("ao3_listing.html"))

        async with make_source(handler) as source:

            async def read(query):
                async with source.request_context(priority="background"):
                    return await source.search(query)

            running = asyncio.create_task(read("running"))
            await asyncio.wait_for(entered.wait(), 1)
            cancelled = asyncio.create_task(read("cancelled"))
            background = asyncio.create_task(read("background"))
            promoted = asyncio.create_task(read("promoted"))
            await asyncio.sleep(0)
            assert source.promote(promoted)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            release.set()
            await asyncio.gather(running, background, promoted)
            await source.search("later")

    asyncio.run(run())
    assert seen == ["running", "promoted", "background", "later"]


def test_repeated_transport_failures_open_bounded_cooldown_then_recover(monkeypatch):
    seen = []
    module = importlib.import_module("mini_notes.source")
    clock = [100.0]
    from types import SimpleNamespace

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def handler(request):
        seen.append(request)
        if len(seen) <= 4:
            raise httpx.ReadTimeout("private transport details", request=request)
        return httpx.Response(200, text=fixture("ao3_listing.html"))

    async def run():
        async with make_source(handler, retry_delay=0) as source:
            for _ in range(2):
                with pytest.raises(Exception) as error:
                    await source.search("retry")
                assert error.value.code == "network"
            with pytest.raises(Exception) as error:
                await source.search("cooling")
            assert error.value.code == "network" and error.value.retry_after > 0
            assert len(seen) == 4
            assert source.health["circuit_remaining_seconds"] > 0
            clock[0] += 11
            assert (await source.search("recovered")).items
            assert source.health["last_error"] is None
            assert source.health["consecutive_network_failures"] == 0
            assert source.health["retries"] == 2

    asyncio.run(run())
    assert len(seen) == 5


def test_language_options_use_actual_ao3_ids_without_inventing_script_filters():
    from mini_notes.models import LANGUAGE_OPTIONS, SearchFilters, WorkSummary, matches_filters

    assert LANGUAGE_OPTIONS["zh"] == "中文-普通话 國語"
    assert "zh-Hant" not in LANGUAGE_OPTIONS and "zh-Hans" not in LANGUAGE_OPTIONS
    work = WorkSummary("1", BASE + "/works/1", "original", language=LANGUAGE_OPTIONS["zh"])
    assert matches_filters(work, SearchFilters(language="zh"))
    assert not matches_filters(work, SearchFilters(language="en"))


def test_proxy_snapshot_never_returns_credentials_paths_or_no_proxy_contents():
    from mini_notes.network_diagnostics import proxy_snapshot

    result = proxy_snapshot(
        {
            "HTTPS_PROXY": "http://user:private-password@127.0.0.1:9999/private?token=secret",
            "ALL_PROXY": "socks5h://localhost:8888",
            "NO_PROXY": "private.internal,secret.example",
        }
    )
    assert result["HTTPS_PROXY"] == {
        "set": True,
        "scheme": "http",
        "hostname": "127.0.0.1",
        "loopback": True,
    }
    assert result["ALL_PROXY"]["loopback"] is True
    assert result["HTTP_PROXY"] == {"set": False}
    assert result["NO_PROXY"] == {"set": True}
    assert all(x not in repr(result) for x in ["private", "secret", "9999", "user", "token"])
