"""Installed command line entry; the application is a native, persistent TUI."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
import signal
import sys
import time

from .source import AO3Source, SourceError
from .models import SearchFilters, WorkSummary, matches_filters
from .network_diagnostics import network_configuration, public_origin

VERSION = "0.6.0"
DEFAULT_SOURCE = "https://aoya.moe"
LEGACY_DEFAULT_SOURCE = "https://archiveofourown.org"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-ao3", description="Claude 凹3：Claude Code 风格的 AO3 终端摸鱼阅读器")
    p.add_argument("--version", action="version", version=f"Claude 凹3 (claude-ao3) {VERSION}")
    p.add_argument("--source", help="AO3兼容HTTPS域名；默认aoya.moe第三方镜像，不会自动换站")
    p.add_argument("--theme", choices=("auto", "dark", "light"), help="终端配色，默认auto")
    p.add_argument(
        "--language",
        choices=("zh",),
        help=argparse.SUPPRESS,  # Legacy zh scripts still work; product has no language switch.
    )
    p.add_argument("--network-info", action="store_true", help="显示脱敏代理配置，不发送网络请求")
    p.add_argument("--http-client", choices=("auto", "curl", "httpx"), default="auto",
                   help="请求客户端；auto优先系统curl HTTP/1.1，不存在时使用HTTPX")
    p.add_argument("--storage-info", action="store_true", help="只读查看阅读数据占用与预算，不读取正文")
    p.add_argument(
        "--network", choices=("system", "direct"), default="direct",
        help="direct禁用应用代理；system继承环境代理；不自动切换，默认direct",
    )
    p.add_argument("--data-dir", type=Path, help="本地阅读进度与缓存目录")
    p.add_argument("--open", dest="open_url", help="启动时直接打开当前书源的公开作品或章节URL")
    p.add_argument(
        "--doctor", action="store_true", help="检查真实搜索和正文接入，输出无正文JSON后退出"
    )
    p.add_argument("--timeout", type=float, default=20, help="单次请求等待预算，默认20秒；前台操作总预算45秒")
    return p


async def diagnose(source_url: str, timeout: float, network: str = "system", *, deadline: float = 45, http_client: str = "auto") -> tuple[dict, int]:
    report = {"source": public_origin(source_url), "version": VERSION, "network": network,
              "deadline_seconds": deadline, "checks": [], "requests": [], "ok": False,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "network_configuration": network_configuration(network, http_client),
              "mainland_direct_verified": False,
              "scope": "Single-process search/body check. Does not verify location, VPN/TUN status or nationwide access.",
              "code_sha256": {f"src/mini_notes/{p.name}": hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(Path(__file__).parent.glob("*.py"))}}
    started = time.perf_counter()
    request_started = started

    async def on_request(request):
        nonlocal request_started
        request_started = time.perf_counter()
        report["requests"].append({"host": request.url.host, "path": request.url.path,
                                   "status": None, "outcome": "pending"})

    async def on_response(response):
        report["requests"][-1].update(
            status=response.status_code, protocol=response.http_version, outcome="http_response",
            response_headers_ms=round((time.perf_counter() - request_started) * 1000, 1))

    def record_failure(kind):
        if report["requests"]:
            report["requests"][-1].update(outcome=kind,
                elapsed_ms=round((time.perf_counter() - request_started) * 1000, 1))

    try:
        async with asyncio.timeout(deadline), AO3Source(source_url, timeout=timeout, network=network, http_client=http_client, max_retries=0) as source:
            report["transport"] = getattr(source, "transport_name", "custom")
            client = getattr(source, "_client", None)
            if client is not None:
                client.event_hooks["request"].append(on_request)
                client.event_hooks["response"].append(on_response)
            t = time.perf_counter()
            filters = SearchFilters(warnings=["16"], categories=["21"], rating="10", language="zh")
            page = await source.search(
                warnings=filters.warnings, categories=filters.categories,
                rating=filters.rating, language=filters.language,
            )
            report["checks"].append(
                {
                    "step": "public_search",
                    "items": len(page.items),
                    "has_next_page": bool(page.next_url),
                    "milliseconds": round((time.perf_counter() - t) * 1000, 1),
                }
            )
            if not page.items:
                raise SourceError("no_results", "诊断筛选未返回作品；可在TUI中使用其他检索条件。")
            t = time.perf_counter()
            detail = await source.get_work(page.items[0].url)
            chapter = detail.chapter
            if not matches_filters(chapter.work, filters):
                raise SourceError("filter_mismatch", "返回作品不符合中文及诊断筛选条件。")
            if not any(p.strip() for p in chapter.paragraphs):
                raise SourceError("parse_error", "来源没有可读正文。")
            report["checks"].append(
                {
                    "step": "public_body",
                    "work_id": chapter.work.id,
                    "chapter_id": chapter.id,
                    "language_id": chapter.work.language_id,
                    "language": chapter.work.language,
                    "paragraphs": len(chapter.paragraphs),
                    "characters": sum(len(p) for p in chapter.paragraphs),
                    "body_sha256": hashlib.sha256("\n".join(chapter.paragraphs).encode()).hexdigest(),
                    "has_next_chapter": bool(chapter.next_url),
                    "milliseconds": round((time.perf_counter() - t) * 1000, 1),
                }
            )
            report["ok"] = True
    except SourceError as exc:
        report["error"] = {"code": exc.code, "message": exc.message,
                           "failure_kind": getattr(exc, "failure_kind", None),
                           "http_status": getattr(exc, "http_status", None),
                           "curl_exit_code": getattr(exc, "curl_exit_code", None)}
        record_failure(exc.failure_kind)
    except TimeoutError:
        report["error"] = {"code": "deadline", "message": "诊断达到总等待上限，已取消。"}
        record_failure("deadline")
    except Exception as exc:
        report["error"] = {"code": "unexpected", "message": type(exc).__name__}
        record_failure("unexpected")
    report["milliseconds"] = round((time.perf_counter() - started) * 1000, 1)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report, 0 if report["ok"] else 2


def prepare_language(store, preferences: dict, requested: str | None, *, prepared_state: dict | None = None) -> str:
    """Normalize all discovery and drafts to the product's single Chinese option.

    Keep the current body intact during migration; never translate or delete a
    reader's saved text just because the old application allowed other languages.
    """
    saved = store.load()
    language = "zh"
    if saved:
        old_language = saved.get("filters", {}).get("language", "")
        saved.setdefault("filters", {})["language"] = language
        flow = saved.get("flow") or {}
        flow_language = flow.get("filters", {}).get("language", "")
        flow.setdefault("filters", {})["language"] = language
        for name in ("origin", "recent", "series_queue"):
            queue = flow.get(name)
            if queue and queue.get("filters", {}).get("language", "") != language:
                if name == "origin":
                    # Preserve this reading path's original query, not a newer
                    # search form draft. The engine reopens page one via search().
                    queue.setdefault("filters", {})["language"] = language
                    queue.update(
                        items=[], index=0, url="", next_url=None, visited_urls=[], restart=True
                    )
                else:
                    flow[name] = None
                if name == "series_queue":
                    flow["series_found"] = False
        current = saved.get("current")
        work = (current or {}).get("work") or {}
        current_mismatch = bool(current) and not matches_filters(
            WorkSummary(
                "",
                "",
                "",
                language=work.get("language", ""),
                language_id=work.get("language_id", ""),
            ),
            SearchFilters(language=language),
        )
        if flow_language != language:
            # Prior filter rejections are not permanent user skips. Retain
            # completed-work dedup and explicit skipped-series intent separately.
            flow["skipped"] = []
        if current_mismatch:
            # The saved flow may already say zh while the retained body is from
            # an older language. Validate the body independently on every restore.
            flow.update(
                restart_discovery=True, series_url=None, series_queue=None, series_found=False
            )
        transition = flow_language != language or current_mismatch
        if transition:
            saved["resume_notice"] = (
                "已保留当前正文；后续只检索中文作品。"
                if flow.get("origin") and flow.get("active_kind") == "search"
                else "已保留当前正文；后续只获取中文近期作品。"
            )
        else:
            saved.pop("resume_notice", None)
        saved["flow"] = flow
        if transition and current:
            saved["history"] = [
                {
                    "url": saved["current"]["url"],
                    "position": copy.deepcopy(saved.get("position", {})),
                    "flow": copy.deepcopy(flow),
                    "source_label": saved.get("source_label", "本地恢复"),
                }
            ]
            saved["history_index"] = 0
        if old_language != language:
            saved["results"] = None
            saved["results_selection"], saved["results_page_number"] = 0, 1
        draft = dict(saved.get("search_draft") or saved["filters"])
        draft["language"] = language
        saved["search_draft"] = draft
        if prepared_state is not None:
            prepared_state.update(saved)
        from .storage import StorageError
        try:
            store.save(saved)
        except StorageError:
            # Legacy oversized records remain readable. The store keeps last_error;
            # the engine/UI reports that a new snapshot cannot yet be saved.
            pass
    return language


@contextmanager
def termination_handlers(app):
    """Let a closed terminal request the same save/cleanup as Ctrl+C."""
    previous = {}

    def request_exit(signum, frame):
        try:
            asyncio.get_running_loop().call_soon(app.action_exit_reader)
        except RuntimeError:
            raise KeyboardInterrupt

    try:
        for name in ("SIGHUP", "SIGTERM"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, request_exit)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: list[str] | None = None) -> None:
    p = parser()
    args = p.parse_args(argv)
    if args.storage_info:
        from platformdirs import user_data_path
        from .storage_policy import reading_storage_usage, StorageBudgetError
        try:
            print(json.dumps(reading_storage_usage(args.data_dir or user_data_path("mini-notes", appauthor=False)), ensure_ascii=False, indent=2))
        except StorageBudgetError as exc:
            p.exit(2, str(exc) + "\n")
        return
    if args.network_info:
        print(
            json.dumps(
                {
                    "source": public_origin(args.source or DEFAULT_SOURCE),
                    **network_configuration(args.network, args.http_client),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not 1 <= args.timeout <= 120:
        p.error("--timeout 须在1到120秒之间")
    if args.doctor:
        result, code = asyncio.run(diagnose(args.source or DEFAULT_SOURCE, args.timeout, args.network, http_client=args.http_client))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(code)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        p.exit(2, "请在交互终端运行 claude-ao3；自动检查请使用 --doctor。\n")

    from .app import MiniNotesApp
    from .engine import ReaderEngine
    from .storage import StateStore, SessionLock, SessionInUseError, StorageError
    from .storage_policy import check_settings_budget, check_storage_budget, reading_storage_usage, StorageBudgetError

    exit_code = 0
    try:
        with ExitStack() as cleanup:
            # Acquire before opening SQLite: a second window must not migrate,
            # vacuum or purge the active window's database.
            session_lock = SessionLock(args.data_dir)
            cleanup.callback(session_lock.close)
            check_settings_budget(session_lock.path.parent)
            settings_store = StateStore(session_lock.path.parent)
            cleanup.callback(settings_store.close)
            preferences = settings_store.get_preferences()
            saved_source = preferences.get("source")
            source_url = args.source or (
                DEFAULT_SOURCE if saved_source in (None, LEGACY_DEFAULT_SOURCE) else saved_source
            )
            theme = args.theme or preferences.get("theme", "auto")
            if theme not in ("auto", "dark", "light"):
                theme = "auto"
            # Foreground/App and background/Engine own visible recovery budgets.
            # Disable transport-level retries here so nested layers do not multiply attempts.
            source = AO3Source(source_url, timeout=args.timeout, network=args.network, max_retries=0, http_client=args.http_client)

            def close_source():
                # Textual normally closes this in its own loop. A constructor/UI
                # failure still gets a bounded best-effort transport cleanup.
                if not getattr(source, "_closed", False):
                    with suppress(Exception):
                        asyncio.run(asyncio.wait_for(source.aclose(), timeout=2))

            cleanup.callback(close_source)
            namespace = hashlib.sha256(source.base_url.encode()).hexdigest()[:16]
            check_storage_budget(settings_store.data_dir, namespace)
            state = StateStore(settings_store.data_dir / "sources" / namespace, purge_cache=True)
            cleanup.callback(state.close)
            prepared_state: dict = {}
            language = prepare_language(state, preferences, args.language, prepared_state=prepared_state)
            initial_url = args.open_url

            class StartupEngine(ReaderEngine):
                async def start(self):
                    if initial_url:
                        return await self.open_url(initial_url)
                    return await super().start()

            engine = StartupEngine(source, state, initial_state=prepared_state or None)
            engine.storage_summary = lambda: reading_storage_usage(settings_store.data_dir)
            engine.filters.language = language
            app = MiniNotesApp(engine, theme_mode=theme, source_url=source.base_url)
            try:
                with termination_handlers(app):
                    app.run()
            except KeyboardInterrupt:
                pass
            finally:
                exit_code = getattr(app, "return_code", 0) or 0
                # Snapshot even if app.run/engine.close failed before its normal
                # save. If startup failed, don't overwrite a valid prior chapter.
                snapshot = (
                    engine.resume_snapshot()
                    if engine.current or not state.load().get("current")
                    else None
                )
                try:
                    try:
                        state.compact_on_exit(snapshot)
                    except StorageError as exc:
                        # Failed writes preserve the previous valid snapshot; still
                        # clear temporary bodies and report the unsaved chapter.
                        exit_code = exit_code or 2
                        print(f"当前内容未保存：{exc}", file=sys.stderr)
                        with suppress(StorageError):
                            state.compact_on_exit()
                finally:
                    settings_store.set_preferences(
                        {
                            "source": source.base_url,
                            "theme": app.theme_mode,
                            "language": engine.filters.language,
                        }
                    )
    except SessionInUseError as exc:
        p.exit(2, str(exc) + "\n")
    except SourceError as exc:
        p.exit(2, exc.message + "\n")
    except (StorageError, StorageBudgetError) as exc:
        p.exit(2, str(exc) + "\n")
    if exit_code:
        raise SystemExit(exit_code)
