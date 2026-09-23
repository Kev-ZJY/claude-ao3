import pytest

from mini_notes.cli import diagnose, main
from mini_notes.source import SourceError


def test_no_tty_has_clear_instruction(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exit:
        main([])
    assert exit.value.code == 2
    assert "--doctor" in capsys.readouterr().err


def test_version_needs_no_network_or_store(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["--version"])
    assert exit.value.code == 0
    assert "Claude 凹3 (claude-ao3) 0.6.0" in capsys.readouterr().out


async def test_diagnostics_failure_is_structured_and_no_fallback(monkeypatch):
    origins = []

    class DeniedSource:
        def __init__(self, origin, **kwargs):
            origins.append(origin)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def search(self, **kwargs):
            raise SourceError("forbidden", "书源拒绝访问")

    monkeypatch.setattr("mini_notes.cli.AO3Source", DeniedSource)
    result, code = await diagnose("https://www.ao3-cn.com", 10)
    assert code == 2 and not result["ok"]
    assert result["error"]["code"] == "forbidden"
    assert origins == ["https://www.ao3-cn.com"]


def test_network_diagnostic_is_explicit_read_only_and_does_not_leak_proxy(monkeypatch, capsys):
    import json

    monkeypatch.setenv("HTTPS_PROXY", "http://private-user:secret@127.0.0.1:12345")
    main(["--network-info", "--network", "direct"])
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["network"] == "direct" and not report["environment_proxy_used"]
    assert report["proxy_environment"]["HTTPS_PROXY"]["loopback"]
    assert "secret" not in output and "private-user" not in output and "12345" not in output


@pytest.mark.parametrize("saved,explicit,expected", [
    (None, None, "https://aoya.moe"),
    ("https://archiveofourown.org", None, "https://aoya.moe"),
    ("https://go3-cn.online", None, "https://go3-cn.online"),
    ("https://aoya.moe", "https://archiveofourown.org", "https://archiveofourown.org"),
])
def test_default_source_migration_keeps_custom_choices_and_uses_direct_network(
    tmp_path, monkeypatch, saved, explicit, expected
):
    from mini_notes.storage import StateStore

    if saved:
        store = StateStore(tmp_path)
        store.set_preferences({"source": saved})
        store.close()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    observed = {}

    class InspectApp:
        def __init__(self, engine, theme_mode, **kwargs):
            self.engine, self.theme_mode = engine, theme_mode

        def action_exit_reader(self):
            pass

        def run(self):
            observed.update(source=self.engine.source.base_url, network=self.engine.source.network)

    monkeypatch.setattr("mini_notes.app.MiniNotesApp", InspectApp)
    argv = ["--data-dir", str(tmp_path)]
    if explicit:
        argv.extend(["--source", explicit])
    main(argv)
    assert observed == {"source": expected, "network": "direct"}
    store = StateStore(tmp_path)
    assert store.get_preferences()["source"] == expected
    store.close()


async def test_doctor_has_total_deadline_and_closes_hanging_source(monkeypatch):
    import asyncio

    options = {}

    class HangingSource:
        def __init__(self, origin, **kwargs):
            options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            options["closed"] = True

        async def search(self, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr("mini_notes.cli.AO3Source", HangingSource)
    report, code = await diagnose("https://reader.invalid", 12, "direct", deadline=.01)
    assert code == 2 and report["error"]["code"] == "deadline"
    assert options["network"] == "direct" and options["closed"]


def test_language_migration_keeps_query_but_drops_old_language_urls(tmp_path):
    from mini_notes.cli import prepare_language
    from mini_notes.storage import StateStore

    store = StateStore(tmp_path)
    filters = {"query": "original rain", "warnings": ["16"], "language": ""}
    store.save(
        {
            "filters": filters,
            "flow": {
                "filters": filters,
                "active_kind": "search",
                "origin": {
                    "kind": "search",
                    "filters": filters,
                    "url": "https://reader.example/old-all",
                    "next_url": "https://reader.example/old-all?page=2",
                    "items": [],
                    "index": 0,
                },
            },
            "results": {"url": "https://reader.example/old-all", "items": []},
            "search_draft": {"query": "unsubmitted draft", "language": "en"},
        }
    )
    assert prepare_language(store, {}, None) == "zh"
    saved = store.load()
    origin = saved["flow"]["origin"]
    assert origin["filters"]["query"] == "original rain"
    assert origin["filters"]["language"] == "zh" and origin["restart"]
    assert not origin["url"] and origin["next_url"] is None
    assert saved["results"] is None
    assert saved["search_draft"] == {"query": "unsubmitted draft", "language": "zh"}
    store.close()


@pytest.mark.parametrize("language", ["", "en", "zh"])
def test_legacy_language_is_forced_to_chinese_and_only_chinese_queue_retained(tmp_path, language):
    from mini_notes.cli import prepare_language
    from mini_notes.storage import StateStore

    store = StateStore(tmp_path)
    saved = {
        "filters": {"query": "q", "language": language},
        "flow": {
            "filters": {"language": language},
            "origin": {"filters": {"query": "q", "language": language}, "next_url": "cursor"},
        },
    }
    store.save(saved)
    assert prepare_language(store, {"language": language}, None) == "zh"
    assert store.load()["flow"]["origin"]["next_url"] == ("cursor" if language == "zh" else None)
    store.close()


def test_app_failure_still_saves_latest_compact_state_and_releases_session(tmp_path, monkeypatch):
    import hashlib
    from mini_notes.cli import DEFAULT_SOURCE
    from mini_notes.models import Chapter, WorkSummary
    from mini_notes.storage import SessionLock, StateStore

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    class FailingApp:
        def __init__(self, engine, theme_mode, **kwargs):
            self.engine, self.theme_mode = engine, theme_mode

        def action_exit_reader(self):
            pass

        def run(self):
            engine = self.engine
            work = WorkSummary("1", DEFAULT_SOURCE + "/works/1", "原创恢复样本")
            engine.current = Chapter("2", work.url + "/chapters/2", "原创章", ["原创正文"], work)
            engine.set_position({"paragraph": 0, "offset": 3})
            engine.search_draft = {"query": "latest unsubmitted"}
            engine.store.cache_put("speculative", {"paragraphs": ["不保留的预取正文"]})
            raise RuntimeError("synthetic UI failure")

    monkeypatch.setattr("mini_notes.app.MiniNotesApp", FailingApp)
    with pytest.raises(RuntimeError, match="synthetic UI failure"):
        main(["--data-dir", str(tmp_path)])
    namespace = hashlib.sha256(DEFAULT_SOURCE.encode()).hexdigest()[:16]
    store = StateStore(tmp_path / "sources" / namespace)
    assert store.load()["current"]["id"] == "2"
    assert store.load()["position"] == {"paragraph": 0, "offset": 3}
    assert store.load()["search_draft"]["query"] == "latest unsubmitted"
    assert store.cache_info()["entries"] == 0
    store.close()
    SessionLock(tmp_path).close()


@pytest.mark.parametrize("app_code", [0, 7])
def test_cli_preserves_app_exit_code_after_final_save(tmp_path, monkeypatch, app_code):
    import hashlib
    from mini_notes.cli import DEFAULT_SOURCE
    from mini_notes.models import Chapter, WorkSummary
    from mini_notes.storage import SessionLock, StateStore

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    class FinishedApp:
        return_code = app_code

        def __init__(self, engine, theme_mode, **kwargs):
            self.engine, self.theme_mode = engine, theme_mode

        def action_exit_reader(self):
            pass

        def run(self):
            work = WorkSummary("1", DEFAULT_SOURCE + "/works/1", "自拟退出样本")
            self.engine.current = Chapter("2", work.url + "/chapters/2", "自拟章", ["自拟正文"], work)
            self.engine.search_draft = {"query": "离开前的草稿"}

    monkeypatch.setattr("mini_notes.app.MiniNotesApp", FinishedApp)
    if app_code:
        with pytest.raises(SystemExit) as exc:
            main(["--data-dir", str(tmp_path)])
        assert exc.value.code == app_code
    else:
        assert main(["--data-dir", str(tmp_path)]) is None
    namespace = hashlib.sha256(DEFAULT_SOURCE.encode()).hexdigest()[:16]
    store = StateStore(tmp_path / "sources" / namespace)
    assert store.load()["current"]["id"] == "2"
    assert store.load()["search_draft"]["query"] == "离开前的草稿"
    store.close()
    SessionLock(tmp_path).close()


def test_cli_final_write_failure_is_nonzero_and_keeps_last_snapshot(tmp_path, monkeypatch, capsys):
    from dataclasses import asdict
    import hashlib
    from mini_notes.cli import DEFAULT_SOURCE
    from mini_notes.models import Chapter, WorkSummary
    from mini_notes.storage import SessionLock, StateStore

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    namespace = hashlib.sha256(DEFAULT_SOURCE.encode()).hexdigest()[:16]
    store = StateStore(tmp_path / "sources" / namespace)
    previous_work = WorkSummary("1", DEFAULT_SOURCE + "/works/1", "自拟已保存样本", language_id="zh")
    previous_chapter = Chapter("1", previous_work.url + "/chapters/1", "上次阅读", ["自拟旧正文"], previous_work)
    previous = {
        "filters": {"language": "zh"}, "flow": {"filters": {"language": "zh"}},
        "search_draft": {"query": "上次已保存", "language": "zh"},
        "current": asdict(previous_chapter), "position": {"paragraph": 0, "offset": 3},
    }
    store.save(previous)
    store.close()

    class FinishedApp:
        return_code = 0

        def __init__(self, engine, theme_mode, **kwargs):
            self.engine, self.theme_mode = engine, theme_mode

        def action_exit_reader(self):
            pass

        def run(self):
            work = WorkSummary("1", DEFAULT_SOURCE + "/works/1", "自拟未保存样本")
            self.engine.current = Chapter("2", work.url + "/chapters/2", "自拟章", ["自拟正文"], work)
            self.engine.store.cache_put("prepared", {"paragraphs": ["临时正文"]})
            # SQLite aborts the replacement transaction; the prior row must survive.
            self.engine.store._db.execute("""
                CREATE TRIGGER reject_session_write BEFORE INSERT ON state
                WHEN NEW.key = 'session'
                BEGIN SELECT RAISE(ABORT, 'synthetic disk write failure'); END
            """)

    monkeypatch.setattr("mini_notes.app.MiniNotesApp", FinishedApp)
    with pytest.raises(SystemExit) as exc:
        main(["--data-dir", str(tmp_path)])
    assert exc.value.code == 2
    assert "当前内容未保存" in capsys.readouterr().err
    store = StateStore(tmp_path / "sources" / namespace)
    assert store.load() == previous
    assert store.cache_info()["entries"] == 0
    store.close()
    SessionLock(tmp_path).close()


def test_second_window_is_rejected_before_opening_or_migrating_sqlite(tmp_path, monkeypatch):
    from mini_notes.storage import SessionLock

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    lock = SessionLock(tmp_path)

    def forbidden_store(*args, **kwargs):
        raise AssertionError("second window touched SQLite")

    monkeypatch.setattr("mini_notes.storage.StateStore", forbidden_store)
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--data-dir", str(tmp_path)])
        assert exc.value.code == 2
    finally:
        lock.close()


async def test_termination_handler_schedules_normal_exit_and_restores_handlers():
    import asyncio
    import signal
    from mini_notes.cli import termination_handlers

    class App:
        exits = 0

        def action_exit_reader(self):
            self.exits += 1

    app = App()
    before = signal.getsignal(signal.SIGTERM)
    with termination_handlers(app):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert app.exits == 1
    assert signal.getsignal(signal.SIGTERM) == before


def test_storage_info_never_creates_directory_or_reads_content(tmp_path, capsys):
    import json
    path=tmp_path/'not-created'
    main(['--storage-info','--data-dir',str(path)])
    report=json.loads(capsys.readouterr().out)
    assert report['managed_file_bytes']==0
    assert report['managed_budget_bytes']==161*1024*1024
    assert not path.exists()


def test_oversized_legacy_language_migration_preserves_body_and_can_continue(tmp_path):
    import json
    from mini_notes.cli import prepare_language
    from mini_notes.storage import StateStore
    store=StateStore(tmp_path)
    saved={'filters':{'language':'en'},'flow':{'filters':{'language':'en'}},'current':{'url':'https://reader.invalid/works/1','paragraphs':['原'*(1500000)]}}
    text=json.dumps(saved,ensure_ascii=False)
    with store._db:
        store._db.execute("INSERT INTO state VALUES('session',?)",(text,))
    prepared={}
    assert prepare_language(store,{'language':'en'},'zh',prepared_state=prepared)=='zh'
    assert store.load()['current']['paragraphs']==saved['current']['paragraphs']
    assert store.load()['filters']['language']=='en'
    assert prepared['filters']['language']=='zh'
    assert prepared['flow']['filters']['language']=='zh'
    assert store.last_error['scope']=='session'
    store.close()


def test_total_budget_is_checked_before_settings_creation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin.isatty',lambda:True)
    monkeypatch.setattr('sys.stdout.isatty',lambda:True)
    for i in range(3):
        path=tmp_path/'sources'/f'{i:016x}'/'reader.sqlite3'
        path.parent.mkdir(parents=True)
        with path.open('wb') as f:
            f.truncate(32*1024*1024)
    with pytest.raises(SystemExit) as exit:
        main(['--data-dir',str(tmp_path)])
    assert exit.value.code==2
    assert '未打开或迁移' in capsys.readouterr().err
    assert not (tmp_path/'reader.sqlite3').exists()


@pytest.mark.parametrize("language", ["en", "all", "yue", "wuu", "hak", "nan"])
def test_cli_rejects_removed_language_options(language, capsys):
    from mini_notes.cli import parser
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["--language", language])
    assert exc.value.code == 2
    assert "--language" not in parser().format_help()


async def test_doctor_uses_same_chinese_filter_as_product(monkeypatch):
    seen = {}
    class ProbeSource:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def search(self, **kw):
            seen.update(kw)
            raise SourceError("forbidden", "stop after observing filter")
    monkeypatch.setattr("mini_notes.cli.AO3Source", ProbeSource)
    await diagnose("https://reader.invalid", 1)
    assert seen["language"] == "zh"


def test_already_chinese_flow_with_english_current_restarts_without_losing_body(tmp_path):
    from mini_notes.cli import prepare_language
    from mini_notes.storage import StateStore

    store = StateStore(tmp_path)
    current = {
        "url": "https://reader.invalid/works/1",
        "paragraphs": ["Original English paragraph retained completely."],
        "work": {"language": "English", "language_id": "en"},
    }
    position = {"paragraph": 0, "offset": 12}
    store.save({
        "schema_version": 1, "filters": {"language": "zh", "query": "old query"},
        "current": current, "position": position,
        "search_draft": {"language": "en", "query": "unsubmitted draft"},
        "flow": {"filters": {"language": "zh"}, "active_kind": "search",
                 "origin": {"filters": {"language": "zh", "query": "original query"},
                            "items": [], "next_url": "chinese-cursor"},
                 "series_url": "https://reader.invalid/series/2", "series_found": True},
    })
    assert prepare_language(store, {}, None) == "zh"
    saved = store.load()
    assert saved["current"] == current and saved["position"] == position
    assert saved["flow"]["restart_discovery"]
    assert saved["flow"]["series_url"] is None and not saved["flow"]["series_found"]
    assert saved["flow"]["origin"]["filters"]["query"] == "original query"
    assert saved["search_draft"] == {"language": "zh", "query": "unsubmitted draft"}
    assert saved["history"][0]["position"] == position
    # Reopening before the next successful discovery must keep this intent.
    prepare_language(store, {}, None)
    assert store.load()["flow"]["restart_discovery"]
    store.close()


@pytest.mark.parametrize("language,category,expected_ok", [
    ("en", "Gen", False), ("zh", "M/M", False), ("zh", "Gen", True),
])
async def test_doctor_validates_returned_body_language_and_hard_filters(
    monkeypatch, language, category, expected_ok
):
    from mini_notes.models import Chapter, Page, WorkDetail, WorkSummary

    work = WorkSummary(
        "1", "https://reader.invalid/works/1", "原创诊断作品",
        rating="General Audiences", warnings=["No Archive Warnings Apply"],
        categories=[category], language_id=language,
        language="中文-普通话 國語" if language == "zh" else "English",
    )
    chapter = Chapter("1", work.url, "原创章节", ["原创诊断段落。"], work)

    class ProbeSource:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def search(self, **kwargs):
            assert kwargs["language"] == "zh"
            return Page([work], "https://reader.invalid/works/search")

        async def get_work(self, url):
            return WorkDetail(work, chapter)

    monkeypatch.setattr("mini_notes.cli.AO3Source", ProbeSource)
    report, code = await diagnose("https://reader.invalid", 1)
    assert report["ok"] is expected_ok and code == (0 if expected_ok else 2)
    if expected_ok:
        assert report["checks"][-1]["language_id"] == "zh"
        assert report["checks"][-1]["language"] == "中文-普通话 國語"
    else:
        assert report["error"]["code"] == "filter_mismatch"
        assert not any(check["step"] == "public_body" for check in report["checks"])
