"""Transactional, asynchronous reading state; independent of the terminal UI.

Only a successfully loaded chapter replaces the visible one. Queue exploration
runs on a copy, so source errors/cancellation cannot consume the reading cursor.
The source owns HTTP restrictions; only an explicit UI action confirms a content notice.
"""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import asdict, dataclass, field, fields
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar

from .models import (
    Chapter,
    ChapterRef,
    Page,
    SearchFilters,
    SeriesDetail,
    SeriesRef,
    WorkDetail,
    WorkSummary,
    matches_filters,
)
from .source import SourceError
from .storage import StorageError


_speculation: ContextVar[tuple | None] = ContextVar("reader_speculation", default=None)
_cache_only: ContextVar[object | None] = ContextVar("reader_cache_only", default=None)


def _record(cls, data: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def _chapter(data: dict) -> Chapter:
    value = dict(data)
    value["work"] = _record(WorkSummary, value["work"])
    value["series"] = [_record(SeriesRef, s) for s in value.get("series", [])]
    return _record(Chapter, value)


def _page(data: dict | None) -> Page | None:
    if data is None:
        return None
    value = dict(data)
    value["items"] = [_record(WorkSummary, w) for w in value.get("items", [])]
    return _record(Page, value)


def _work_detail(data: dict) -> WorkDetail:
    return WorkDetail(
        _record(WorkSummary, data["work"]),
        _chapter(data["chapter"]),
        [_record(ChapterRef, c) for c in data.get("chapters", [])],
        [_record(SeriesRef, s) for s in data.get("series", [])],
    )


@dataclass
class _Queue:
    kind: str
    filters: SearchFilters
    items: list[WorkSummary] = field(default_factory=list)
    index: int = 0
    url: str = ""
    next_url: str | None = None
    visited_urls: list[str] = field(default_factory=list)
    restart: bool = False

    @classmethod
    def from_page(cls, kind: str, filters: SearchFilters, page: Page, index=0):
        return cls(
            kind,
            copy.deepcopy(filters),
            copy.deepcopy(page.items),
            index,
            page.url,
            page.next_url,
            [page.url],
        )


@dataclass
class _Flow:
    filters: SearchFilters = field(default_factory=SearchFilters)
    origin: _Queue | None = None
    recent: _Queue | None = None
    active_kind: str = "direct"
    series_url: str | None = None
    series_queue: _Queue | None = None
    series_found: bool = False
    chapters: list[ChapterRef] = field(default_factory=list)
    series_refs: list[SeriesRef] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    chapter_path: list[str] = field(default_factory=list)
    skipped_series: list[str] = field(default_factory=list)
    restart_discovery: bool = False
    revisit_series: bool = False
    predecessor_path: list[str] = field(default_factory=list)


def _decode_queue(data: dict | None) -> _Queue | None:
    if data is None:
        return None
    value = dict(data)
    value["filters"] = _record(SearchFilters, value.get("filters", {}))
    value["items"] = [_record(WorkSummary, w) for w in value.get("items", [])]
    return _record(_Queue, value)


def _decode_flow(data: dict) -> _Flow:
    value = dict(data)
    value["filters"] = _record(SearchFilters, value.get("filters", {}))
    for key in ("origin", "recent", "series_queue"):
        value[key] = _decode_queue(value.get(key))
    value["chapters"] = [_record(ChapterRef, c) for c in value.get("chapters", [])]
    value["series_refs"] = [_record(SeriesRef, s) for s in value.get("series_refs", [])]
    return _record(_Flow, value)


@dataclass
class _Prepared:
    chapter: Chapter | None
    flow: _Flow
    finished: dict[str, dict]
    label: str = ""
    notice: str = ""


class _BudgetReached(Exception):
    pass


class _PreparationPaused(Exception):
    pass


class _CacheMiss(Exception):
    pass


@dataclass
class _Budget:
    pages: int = 0
    candidates: int = 0
    requests: int = 0
    request_limit: int = 8
    page_limit: int = 3
    candidate_limit: int = 30
    started: float = field(default_factory=time.monotonic)

    def request(self):
        if self.requests >= self.request_limit:
            raise _BudgetReached
        self.requests += 1

    def page(self):
        if self.pages >= self.page_limit:
            raise _BudgetReached
        self.pages += 1

    def candidate(self):
        if self.candidates >= self.candidate_limit:
            raise _BudgetReached
        self.candidates += 1


class ReaderEngine:
    """Public navigation methods are async; set_position only updates memory.

    Navigation and close save progress automatically. UI may call save explicitly
    for autosave. SourceError is intentionally exposed, with the old page intact.
    prefetch quietly retains its error for the next explicit navigation request.
    """

    PREFETCH_RETRY_DELAYS = (2.0, 5.0)
    PREFETCH_SECONDS = 45.0

    def __init__(self, source, store, *, initial_state: dict | None = None):
        self.source = source
        self.store = store
        # CLI migrations may be valid in memory even when the old session is
        # too large to replace on disk. All initial restore paths use one copy.
        self._initial_state = copy.deepcopy(initial_state)
        self.current: Chapter | None = None
        self.results: Page | None = None
        self._result_pages: list[Page] = []
        self._result_selections: list[int] = []
        self._result_page_index = 0
        self._result_page_base = 1
        self._results_checked = False
        self.filters = SearchFilters()
        self.search_draft: dict = {}
        self.source_label = "最近更新"
        self.notice = ""
        self.storage_notice = ""
        self.pending_url: str | None = None
        self.position: dict = {}
        self._flow = _Flow()
        self._history: list[dict] = []
        self._history_index = -1
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._finished: dict[str, dict] = {}
        self._epoch = 0
        self._advancing = False
        self._advancing_epoch = -1
        self._closed = False
        self._prefetch_task: asyncio.Task | None = None
        self._prefetch_epoch = -1
        self._prefetch_ready: list[_Prepared] = []
        self._prefetch_error: SourceError | None = None
        self._prefetch_error_after_work: str | None = None
        self._prefetch_terminal: _Prepared | None = None
        self._prefetch_changed = asyncio.Event()
        self._previous_ready: _Prepared | None = None
        self._branch_ready: dict[str, _Prepared] = {}
        self._branch_roots: list[_Prepared] = []
        self._branch_units: dict[str, list[Chapter]] = {}
        self._branch_errors: dict[str, SourceError] = {}
        self._branch_done: set[str] = set()
        self._prefetch_base: Chapter | None = None
        self._prefetch_complete = False
        self._prefetch_budget_limited = False
        self._prefetch_retry_at = 0.0
        self._prefetch_retry_attempt = 0
        self._prefetch_started = 0.0
        self._prefetch_cooldown_until = 0.0
        self._prefetch_generation = 0
        self._prefetch_paused = False
        self._prefetch_retired: set[asyncio.Task] = set()
        self._prefetch_focus: str | None = None
        self._background_calls: dict[tuple, tuple[asyncio.Future, asyncio.Task]] = {}
        self._foreground_resolver: tuple[int, asyncio.Task] | None = None
        self._foreground_settled = asyncio.Event()
        self._foreground_settled.set()

    @property
    def selected_series(self) -> str | None:
        return self._flow.series_url

    @property
    def series(self) -> list[SeriesRef]:
        return self._flow.series_refs

    @staticmethod
    def _unit(chapter: Chapter) -> str:
        return f"{chapter.work.id}:{chapter.id}"

    @staticmethod
    def _following_chapter(chapter: Chapter, flow: _Flow) -> str | None:
        if chapter.next_url:
            return chapter.next_url
        for index, ref in enumerate(flow.chapters[:-1]):
            if ref.id == chapter.id:
                return flow.chapters[index + 1].url
        return None

    def _begin(self, *, keep_prefetch=False):
        """Invalidate a reading path; ordinary next does not call this method."""
        self._epoch += 1
        self._prefetch_generation += 1
        self._prefetch_paused = False
        self.pending_url = None
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_retired.add(self._prefetch_task)
            self._prefetch_task.add_done_callback(self._prefetch_retired.discard)
            self._prefetch_task.cancel()
        self._prefetch_task = None
        self._prefetch_focus = None
        self._prefetch_epoch = -1
        self._prefetch_ready.clear()
        self._prefetch_error = None
        self._prefetch_error_after_work = None
        self._prefetch_terminal = None
        self._previous_ready = None
        self._branch_ready.clear()
        self._branch_roots.clear()
        self._branch_units.clear()
        self._branch_errors.clear()
        self._branch_done.clear()
        self._prefetch_base = None
        self._prefetch_complete = False
        self._prefetch_budget_limited = False
        self._prefetch_retry_at = 0.0
        self._prefetch_retry_attempt = 0
        self._prefetch_started = 0.0
        self._prefetch_cooldown_until = 0.0
        self._prefetch_changed.set()
        self._prefetch_changed = asyncio.Event()
        return self._epoch, None

    @property
    def prefetch_status(self) -> dict:
        ready = len(self._prefetch_ready)
        loading = bool(self._prefetch_task and not self._prefetch_task.done())
        if loading:
            state, message = "loading", f"已预取 {ready}/3 · 正在准备后续内容"
        elif self._prefetch_error or self._branch_errors:
            state, message = "error", f"已预取 {ready}/3 · 后续获取失败，可重试"
        elif ready:
            state, message = "ready", f"已预取 {ready}/3"
        else:
            state, message = (
                "idle",
                ("本轮尚无可预取后继" if self._prefetch_terminal else "尚无已预取内容"),
            )
        base = self.current
        return dict(
            state=state,
            ready=ready,
            target=3,
            message=message,
            previous_ready=int(self._previous_ready is not None),
            current_work_ready=sum(
                1
                for p in self._prefetch_ready
                if p.chapter and base and p.chapter.work.id == base.work.id
            ),
            work_branches=[
                dict(work_id=key, ready=len(value), target=3)
                for key, value in self._branch_units.items()
            ],
            skip_work_ready="skip_work" in self._branch_ready,
            budget_limited=self._prefetch_budget_limited,
            errors=[f"{key}:{exc.code}" for key, exc in self._branch_errors.items()]
            + ([f"next:{self._prefetch_error.code}"] if self._prefetch_error else []),
            retrying=self._prefetch_retry_at > time.monotonic(),
            retry_in=max(
                0.0, max(self._prefetch_retry_at, self._prefetch_cooldown_until) - time.monotonic()
            ),
            cooldown=self._prefetch_cooldown_until > time.monotonic(),
            paused=self._prefetch_paused,
            retry_attempt=self._prefetch_retry_attempt,
            retry_total=3,
            retry_elapsed=max(0.0, time.monotonic() - self._prefetch_started) if loading else 0.0,
        )

    @property
    def results_page_number(self) -> int:
        return self._result_page_base + self._result_page_index

    @property
    def results_selection(self) -> int:
        if self._result_selections:
            return self._result_selections[self._result_page_index]
        return 0

    def set_result_selection(self, index: int):
        if self.results and self._result_selections:
            self._result_selections[self._result_page_index] = max(
                0, min(index, len(self.results.items) - 1)
            )

    @property
    def has_previous_results(self) -> bool:
        return bool(self._result_page_index > 0 or (self.results and self.results.previous_url))

    @property
    def has_next_results(self) -> bool:
        return bool(
            self._result_page_index + 1 < len(self._result_pages)
            or (self.results and self.results.next_url)
        )

    async def _source_call(self, method, *args, **kwargs):
        """Share a matching background fetch; other foreground work uses the source gate."""
        if _cache_only.get() is self:
            raise _CacheMiss
        context = _speculation.get()
        background = context is not None and context[0] is self
        key = (
            context[1] if background else self._epoch,
            context[3] if background else self._prefetch_generation,
            method.__name__, repr(args), repr(sorted(kwargs.items())),
        )
        if not background:
            pending = self._background_calls.get(key)
            if pending:
                promote = getattr(self.source, "promote", None)
                if promote:
                    promote(pending[1])
                return await asyncio.shield(pending[0])
            return await self._fetch_source(method, *args, **kwargs)
        future = asyncio.get_running_loop().create_future()
        # Most speculative calls have no foreground consumer. Retrieve failures
        # to avoid unobserved-future warnings while retaining them for any waiter.
        future.add_done_callback(lambda value: None if value.cancelled() else value.exception())
        self._background_calls[key] = (future, asyncio.current_task())
        try:
            value = await self._fetch_source(method, *args, **kwargs)
            future.set_result(value)
            return value
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
            raise
        finally:
            self._background_calls.pop(key, None)

    async def _fetch_source(self, method, *args, **kwargs):
        context = _speculation.get()
        background = context is not None and context[0] is self
        priority = "background" if background else "foreground"
        manager = getattr(self.source, "request_context", None)
        for attempt in range(3 if background else 1):
            if background:
                if (
                    context[1] != self._epoch
                    or self._closed
                    or context[3] != self._prefetch_generation
                ):
                    raise asyncio.CancelledError
                if self._prefetch_cooldown_until > time.monotonic():
                    raise _PreparationPaused
                if time.monotonic() - context[2].started >= self.PREFETCH_SECONDS:
                    self._prefetch_budget_limited = True
                    raise _PreparationPaused
                context[2].request()
            try:
                async with manager(priority=priority) if manager else nullcontext():
                    if background:
                        remaining = max(
                            0, self.PREFETCH_SECONDS - (time.monotonic() - context[2].started)
                        )
                        try:
                            async with asyncio.timeout(remaining):
                                value = await method(*args, **kwargs)
                            if (
                                context[1] != self._epoch
                                or self._closed
                                or context[3] != self._prefetch_generation
                            ):
                                raise asyncio.CancelledError
                            return value
                        except TimeoutError:
                            exc = SourceError(
                                "network", "后台准备达到45秒上限；已保留准备好的内容。"
                            )
                            exc.retryable, exc.retry_budget_exhausted = True, True
                            exc.recovery_exhausted = True
                            exc.attempts = attempt + 1
                            raise exc from None
                    return await method(*args, **kwargs)
            except SourceError as exc:
                if exc.retry_after is not None:
                    exc.retry_at = time.monotonic() + max(0, exc.retry_after)
                if background and (
                    context[1] != self._epoch
                    or self._closed
                    or context[3] != self._prefetch_generation
                ):
                    raise asyncio.CancelledError from None
                if background:
                    exc.attempts = max(getattr(exc, "attempts", 1), attempt + 1)
                if not background or not getattr(exc, "retryable", False) or attempt == 2:
                    if background and getattr(exc, "retryable", False):
                        exc.retry_budget_exhausted = True
                        exc.recovery_exhausted = True
                        self._prefetch_cooldown_until = max(
                            self._prefetch_cooldown_until,
                            time.monotonic() + (getattr(exc, "retry_after", None) or 0),
                        )
                    raise
                delay = max(
                    self.PREFETCH_RETRY_DELAYS[attempt], getattr(exc, "retry_after", None) or 0
                )
                if time.monotonic() + delay - context[2].started > self.PREFETCH_SECONDS:
                    exc.retry_budget_exhausted = True
                    exc.recovery_exhausted = True
                    self._prefetch_cooldown_until = max(
                        self._prefetch_cooldown_until,
                        time.monotonic() + (getattr(exc, "retry_after", None) or 0),
                    )
                    raise
                self._prefetch_retry_attempt = attempt + 2
                self._prefetch_retry_at = time.monotonic() + delay
                self._prefetch_changed.set()
                try:
                    await asyncio.sleep(delay)
                finally:
                    self._prefetch_retry_at = 0

    @staticmethod
    def _remaining_error(error: SourceError) -> SourceError:
        """A stored relative Retry-After must not start a second cooldown later."""
        if getattr(error, "retry_at", None) is not None:
            error.retry_after = max(0, error.retry_at - time.monotonic())
        return error

    @contextmanager
    def _errors(self, epoch: int):
        """Only the active foreground operation may expose its failure target."""
        try:
            yield
        except SourceError as exc:
            if epoch == self._epoch and not self._closed:
                self.pending_url = getattr(exc, "url", None)
                raise

    @staticmethod
    def _hard_args(filters: SearchFilters):
        return dict(
            warnings=filters.warnings,
            categories=filters.categories,
            rating=filters.rating,
            language=filters.language,
        )

    def _cache_read(self, key: str, decoder):
        if self._closed:
            return None
        value = self.store.cache_get(key)
        if value is not None:
            try:
                return decoder(value)
            except (TypeError, KeyError, ValueError):
                # An old/corrupt record is a cache miss, never an empty result.
                return None
        return None

    def _cache_chapter(self, chapter: Chapter):
        if not self._closed:
            self.store.cache_put("chapter:" + chapter.url, asdict(chapter))

    @staticmethod
    def _validate_chapter(chapter: Chapter):
        if not chapter.id or not any(p.strip() for p in chapter.paragraphs):
            raise SourceError("parse_error", "来源没有提供可读正文，已保留当前页面。")

    async def _get_chapter(self, url: str) -> Chapter:
        epoch = self._epoch
        chapter = self._cache_read("chapter:" + url, _chapter)
        if chapter is None:
            chapter = await self._source_call(self.source.get_chapter, url)
        self._validate_chapter(chapter)
        if epoch == self._epoch:
            self._cache_chapter(chapter)
        return chapter

    async def _get_work(self, url: str, *, fresh=False) -> WorkDetail:
        epoch = self._epoch
        detail = None if fresh else self._cache_read("work:" + url, _work_detail)
        if detail is None:
            detail = await self._source_call(self.source.get_work, url)
            self._validate_chapter(detail.chapter)
            if not self._closed and epoch == self._epoch:
                self.store.cache_put("work:" + url, asdict(detail))
                self.store.cache_put("work:" + detail.work.url, asdict(detail))
        if epoch == self._epoch:
            self._cache_chapter(detail.chapter)
        return detail

    async def _get_series(self, url: str) -> SeriesDetail:
        epoch = self._epoch

        def decode(data):
            value = dict(data)
            value["works"] = [_record(WorkSummary, w) for w in value.get("works", [])]
            return _record(SeriesDetail, value)

        detail = self._cache_read("series:" + url, decode)
        if detail is None:
            detail = await self._source_call(self.source.get_series, url)
            if not self._closed and epoch == self._epoch:
                self.store.cache_put("series:" + url, asdict(detail), ttl=3600)
        return detail

    def set_position(self, anchor: dict):
        self.position = copy.deepcopy(anchor)
        self._remember_current()

    def _remember_current(self):
        if 0 <= self._history_index < len(self._history) and self.current is not None:
            entry = self._history[self._history_index]
            entry["position"] = copy.deepcopy(self.position)
            entry["flow"] = asdict(self._flow)

    def _mark_seen(self, chapter: Chapter):
        unit = self._unit(chapter)
        if unit in self._seen_order:
            self._seen_order.remove(unit)
        self._seen_order.append(unit)
        self._seen.add(unit)
        while len(self._seen_order) > 2048:
            self._seen.discard(self._seen_order.pop(0))

    def snapshot(self, *, include_history=True) -> dict:
        self._remember_current()
        return dict(
            schema_version=1,
            current=asdict(self.current) if self.current else None,
            position=copy.deepcopy(self.position),
            filters=asdict(self.filters),
            search_draft=copy.deepcopy(self.search_draft),
            results=asdict(self.results) if self.results else None,
            source_label=self.source_label,
            flow=asdict(self._flow),
            history=copy.deepcopy(self._history) if include_history else [],
            history_index=self._history_index,
            seen=(sorted(self._seen - set(self._seen_order)) + self._seen_order)[-2048:],
            finished=copy.deepcopy(self._finished),
            results_selection=self.results_selection,
            results_page_number=self.results_page_number,
        )

    def resume_snapshot(self) -> dict:
        """Persist one current body, not prior chapters or speculative bodies."""
        value = self.snapshot(include_history=False)
        value["finished"] = dict(list(value["finished"].items())[-512:])
        flow = value["flow"]
        for name, limit in (
            ("skipped", 1024),
            ("chapter_path", 256),
            ("predecessor_path", 256),
            ("skipped_series", 64),
        ):
            flow[name] = flow.get(name, [])[-limit:]
        # Directory order/URLs remain complete; display titles in this metadata
        # need not duplicate arbitrarily long source headings. The current
        # chapter and every fetched chapter keep their original complete title.
        for ref in flow.get("chapters", []):
            ref["title"] = ref.get("title", "")[:128]
        for ref in flow.get("series_refs", []):
            ref["title"] = ref.get("title", "")[:256]
        for name in ("origin", "recent", "series_queue"):
            queue = flow.get(name)
            if queue:
                queue["items"] = queue["items"][queue["index"] :]
                queue["index"] = 0
                queue["visited_urls"] = queue.get("visited_urls", [])[-128:]
                for work in queue["items"]:
                    # Discovery needs IDs, links and hard-filter metadata. A
                    # second full summary is not required to resume its cursor.
                    work["summary"] = ""
                    work["title"] = work.get("title", "")[:512]
                    work["authors"] = [author[:256] for author in work.get("authors", [])[:8]]
        if self.current:
            value["history"] = [
                dict(
                    url=self.current.url,
                    position=copy.deepcopy(self.position),
                    source_label=self.source_label,
                )
            ]
            value["history_index"] = 0
        else:
            value["history"], value["history_index"] = [], -1
        return value

    def save(self) -> bool:
        if self._closed:
            return False
        try:
            self.store.save(self.resume_snapshot())
        except StorageError as exc:
            self.storage_notice = f"当前进度未保存：{exc}。正文仍保留在本次窗口中。"
            return False
        self._initial_state = None
        self.storage_notice = ""
        return True

    def _load_state(self) -> dict:
        return (
            copy.deepcopy(self._initial_state)
            if self._initial_state is not None
            else self.store.load()
        )

    def restore_results(self) -> Page | None:
        """Restore the committed latest page offline; None is not an empty page."""
        if self.results is not None or self._results_checked:
            return self.results
        saved = self._load_state()
        self._results_checked = True
        if saved.get("schema_version") != 1:
            return None
        try:
            filters = _record(SearchFilters, saved.get("filters", {}))
            page = _page(saved.get("results"))
            number = max(1, int(saved.get("results_page_number", 1)))
            selection = int(saved.get("results_selection", 0))
        except (TypeError, KeyError, ValueError):
            self.notice = "最近搜索记录无法完整恢复，请重新搜索。"
            return None
        self.results, self.filters = page, filters
        if page is None:
            return None
        self._result_pages, self._result_selections = [page], [selection]
        self._result_page_index, self._result_page_base = 0, number
        self.set_result_selection(selection)
        return page

    latest_search_page = restore_results

    def _trim_result_pages(self):
        if len(self._result_pages) > 5:
            left = max(0, min(self._result_page_index - 2, len(self._result_pages) - 5))
            self._result_pages = self._result_pages[left : left + 5]
            self._result_selections = self._result_selections[left : left + 5]
            self._result_page_index -= left
            self._result_page_base += left

    async def start(self) -> Chapter | None:
        saved = self._load_state()
        if saved.get("schema_version") == 1:
            self._results_checked = True
            try:
                self.filters = _record(SearchFilters, saved.get("filters", {}))
                self.search_draft = copy.deepcopy(saved.get("search_draft") or {})
                self.results = _page(saved.get("results"))
                self._result_pages = [self.results] if self.results else []
                self._result_selections = (
                    [int(saved.get("results_selection", 0))] if self.results else []
                )
                self._result_page_index = 0
                self._result_page_base = max(1, int(saved.get("results_page_number", 1)))
                self.set_result_selection(self.results_selection)
            except (TypeError, KeyError, ValueError):
                self.filters, self.results = SearchFilters(), None
                self._result_pages, self._result_selections = [], []
        if saved.get("schema_version") == 1 and saved.get("current"):
            try:
                current = _chapter(saved["current"])
                self._validate_chapter(current)
                flow = _decode_flow(saved.get("flow", {}))
                filters = _record(SearchFilters, saved.get("filters", {}))
                results = _page(saved.get("results"))
            except (TypeError, KeyError, ValueError, SourceError):
                self.notice = "旧记录无法完整恢复，正在获取最近更新。"
            else:
                self._begin()
                self.current, self._flow = current, flow
                self.filters, self.results = filters, results
                self.position = copy.deepcopy(saved.get("position", {}))
                self.source_label = saved.get("source_label", "本地恢复")
                self._history = copy.deepcopy(saved.get("history", []))
                for entry in self._history:
                    entry.setdefault("flow", asdict(flow))
                self._history_index = min(
                    int(saved.get("history_index", -1)), len(self._history) - 1
                )
                self._seen_order = list(dict.fromkeys(saved.get("seen", [])))[-2048:]
                self._seen = set(self._seen_order)
                self._finished = copy.deepcopy(saved.get("finished", {}))
                self._cache_chapter(current)
                self.notice = saved.get("resume_notice") or "已从本地恢复正文与阅读位置。"
                # Re-save the compact representation: recoverable legacy state
                # remains readable if a full current body still exceeds quota.
                self.save()
                return current
        epoch, _ = self._begin()
        with self._errors(epoch):
            flow = _Flow(filters=copy.deepcopy(self.filters), active_kind="recent")
            prepared = await self._resolve(
                None, flow, copy.deepcopy(self._finished), set(self._seen)
            )
            return self._apply(prepared, epoch)

    async def search(self, filters: SearchFilters) -> Page:
        epoch, _ = self._begin()
        chosen = copy.deepcopy(filters)
        try:
            page = await self._source_call(
                self.source.search, query=chosen.query, **self._hard_args(chosen)
            )
        except SourceError as exc:
            if epoch != self._epoch or self._closed:
                return self.results or Page([], "")
            self.pending_url = getattr(exc, "url", None)
            raise
        if epoch == self._epoch and not self._closed:
            self.filters = chosen
            self.results = copy.deepcopy(page)
            self._results_checked = True
            self._result_pages = [self.results]
            self._result_selections = [0]
            self._result_page_index = 0
            self._result_page_base = 1
            self.notice = "" if page.items else "当前条件没有搜索结果；可修改条件或返回阅读。"
            self.save()
        return self.results if epoch != self._epoch and self.results is not None else page

    async def more_results(self) -> Page:
        """Compatibility name: advancing results now displays one real page."""
        return await self.next_results()

    async def next_results(self) -> Page:
        return await self._results_step(1)

    async def prev_results(self) -> Page:
        return await self._results_step(-1)

    async def _results_step(self, direction: int) -> Page:
        if self.results is None:
            return await self.search(self.filters)
        target = self._result_page_index + direction
        epoch, _ = self._begin()
        if 0 <= target < len(self._result_pages):
            self._result_page_index = target
            self.results = self._result_pages[target]
            self.save()
            return self.results
        url = self.results.next_url if direction > 0 else self.results.previous_url
        if not url:
            return self.results
        with self._errors(epoch):
            chosen = copy.deepcopy(self.filters)
            page = await self._source_call(
                self.source.search, query=chosen.query, page_url=url, **self._hard_args(chosen)
            )
            if epoch != self._epoch or self._closed:
                return self.results
            known_urls = {p.url for p in self._result_pages}
            if page.url in known_urls or (direction > 0 and page.next_url in known_urls | {url}):
                raise SourceError("pagination_cycle", "来源分页游标重复，已保留原结果。")
            if direction > 0:
                ids = {w.id for old in self._result_pages for w in old.items}
                page.items = [w for w in page.items if w.id not in ids and not ids.add(w.id)]
                # A source may omit its previous link. The actual visited page is
                # the authoritative return location, never a reconstructed URL.
                page.previous_url = page.previous_url or self.results.url
                self._result_pages.append(page)
                self._result_selections.append(0)
                self._result_page_index += 1
            else:
                self._result_pages.insert(0, page)
                self._result_selections.insert(0, 0)
                self._result_page_base = max(1, self._result_page_base - 1)
            self.results = page
            self._trim_result_pages()
            self.save()
            return page
        return self.results

    def _adopt_work(
        self, flow: _Flow, chapter: Chapter, detail: WorkDetail | None, *, keep_series=False
    ):
        flow.chapter_path = [self._unit(chapter)]
        flow.predecessor_path = []
        flow.chapters = copy.deepcopy(detail.chapters if detail else [])
        flow.series_refs = copy.deepcopy(
            (detail.series if detail else chapter.series) or chapter.series
        )
        if not keep_series:
            flow.series_url = next(
                (ref.url for ref in flow.series_refs if ref.url not in flow.skipped_series), None
            )
            flow.series_queue = None
            flow.series_found = False

    async def _first_chapter(self, detail: WorkDetail) -> Chapter:
        if detail.chapters and detail.chapters[0].url != detail.chapter.url:
            return await self._get_chapter(detail.chapters[0].url)
        return detail.chapter

    def confirm_content_warning(self, url: str) -> None:
        """Keep the committed reading path, but retire failures from before consent."""
        self.source.confirm_content_warning(url)
        self._begin()

    async def open_result(self, index: int) -> Chapter | None:
        if self.results is None or not 0 <= index < len(self.results.items):
            raise SourceError("invalid_selection", "请选择一个有效的搜索结果。")
        self.set_result_selection(index)
        epoch, _ = self._begin()
        with self._errors(epoch):
            results = copy.deepcopy(self.results)
            chosen = copy.deepcopy(self.filters)
            summary = results.items[index]
            detail = await self._get_work(summary.url)
            if not matches_filters(detail.work, chosen):
                raise SourceError("filter_mismatch", "作品元数据不再符合已选择的硬筛选。")
            chapter = await self._first_chapter(detail)
            flow = _Flow(
                chosen, _Queue.from_page("search", chosen, results, index + 1), active_kind="search"
            )
            self._adopt_work(flow, chapter, detail)
            return self._apply(
                _Prepared(chapter, flow, copy.deepcopy(self._finished), "搜索结果"), epoch
            )

    async def open_url(self, url: str) -> Chapter | None:
        # --open can intentionally bypass start(); retain the last committed
        # search instead of overwriting it with an uninitialized None on save.
        if not self._results_checked and self.current is None and not self.search_draft:
            self.search_draft = copy.deepcopy(self._load_state().get("search_draft") or {})
        self.restore_results()
        epoch, _ = self._begin()
        with self._errors(epoch):
            if "/chapters/" in url:
                chapter, detail = await self._get_chapter(url), None
            else:
                detail = await self._get_work(url)
                chapter = await self._first_chapter(detail)
            if not matches_filters(chapter.work, self.filters):
                raise SourceError("filter_mismatch", "作品不符合当前硬筛选；原阅读位置已保留。")
            flow = _Flow(filters=copy.deepcopy(self.filters))
            self._adopt_work(flow, chapter, detail)
            return self._apply(
                _Prepared(chapter, flow, copy.deepcopy(self._finished), "直接打开"), epoch
            )

    async def select_series(self, url: str):
        ref = next((s for s in self._flow.series_refs if s.url == url or s.id == url), None)
        if ref is None:
            raise SourceError("invalid_series", "该系列不属于当前作品。")
        self._begin()
        self._flow.series_url = ref.url
        self._flow.series_queue = None
        self._flow.series_found = False
        self._history = self._history[: self._history_index + 1]
        self.notice = "后续将按所选系列顺序阅读。"
        self.save()

    async def _queue_page(
        self, kind: str, filters: SearchFilters, url: str | None, budget: _Budget
    ):
        budget.page()
        if kind == "series":
            detail = await self._get_series(url)
            return Page(detail.works, detail.url, detail.next_url, detail.previous_url)
        if kind == "search":
            return await self._source_call(
                self.source.search, query=filters.query, page_url=url, **self._hard_args(filters)
            )
        return await self._source_call(self.source.recent, page_url=url, **self._hard_args(filters))

    async def _take(self, queue: _Queue, budget: _Budget) -> WorkSummary | None:
        if queue.restart:
            # A changed language retains the original keyword/hard filters,
            # but restarts from the source's canonical first page, never an old URL.
            page = await self._queue_page(queue.kind, queue.filters, None, budget)
            queue.items, queue.index = page.items, 0
            queue.url, queue.next_url = page.url, page.next_url
            queue.visited_urls, queue.restart = [page.url], False
        while queue.index >= len(queue.items):
            if queue.next_url is None:
                return None
            if queue.next_url in queue.visited_urls:
                raise SourceError("pagination_cycle", "来源返回重复分页，已保留当前正文。")
            page = await self._queue_page(queue.kind, queue.filters, queue.next_url, budget)
            queue.visited_urls = (queue.visited_urls + [queue.next_url])[-128:]
            queue.items, queue.index, queue.url = page.items, 0, page.url
            queue.next_url = page.next_url
        budget.candidate()
        item = queue.items[queue.index]
        queue.index += 1
        return item

    @staticmethod
    def _finished_work(work: WorkSummary, finished: dict[str, dict]) -> bool:
        previous = finished.get(work.id)
        if previous is None:
            return False
        old_count = previous.get("count")
        if (
            work.chapter_count is not None
            and old_count is not None
            and work.chapter_count > old_count
        ):
            return False
        if work.updated and previous.get("updated") and work.updated != previous["updated"]:
            return False
        return True

    async def _eligible_work(
        self,
        summary: WorkSummary,
        flow: _Flow,
        finished: dict[str, dict],
        seen: set[str],
        *,
        include_read=False,
    ):
        if summary.id in flow.skipped or (
            not include_read and self._finished_work(summary, finished)
        ):
            return None
        if not matches_filters(summary, flow.filters):
            flow.skipped.append(summary.id)
            return None
        # Anonymous automatic discovery does not confirm age-gated content.
        # Known ratings avoid fetching many predictable confirmation pages;
        # an explicit open_result/open_url still reaches the source normally.
        if summary.rating in {"Mature", "Explicit"}:
            flow.skipped.append(summary.id)
            return None
        try:
            detail = await self._get_work(summary.url, fresh=summary.id in finished)
            if not matches_filters(detail.work, flow.filters) or any(
                ref.url in flow.skipped_series for ref in detail.series
            ):
                flow.skipped.append(summary.id)
                return None
            refs = detail.chapters or [
                ChapterRef(
                    detail.chapter.id,
                    detail.chapter.url,
                    detail.chapter.title,
                    detail.chapter.position,
                )
            ]
            for ref in refs:
                if include_read or f"{summary.id}:{ref.id}" not in seen:
                    chapter = (
                        detail.chapter
                        if ref.url == detail.chapter.url
                        else await self._get_chapter(ref.url)
                    )
                    self._validate_chapter(chapter)
                    if not matches_filters(chapter.work, flow.filters):
                        flow.skipped.append(summary.id)
                        return None
                    return chapter, detail
        except SourceError as exc:
            # Known unavailable *new works* may be skipped. HTTP errors and
            # malformed responses cannot be turned into an exhausted queue.
            if exc.code not in {"not_found", "adult_confirmation", "login_required"}:
                raise
            flow.skipped.append(summary.id)
        return None

    async def _resolve(
        self,
        current: Chapter | None,
        flow: _Flow,
        finished: dict[str, dict],
        seen: set[str],
        *,
        skip=False,
        budget: _Budget | None = None,
    ) -> _Prepared:
        budget = budget or _Budget()
        skipped_before = len(flow.skipped)
        transitioned = False
        try:
            restart_discovery, flow.restart_discovery = flow.restart_discovery, False
            if restart_discovery:
                current = None
            if current is not None:
                following = self._following_chapter(current, flow)
                if following and not skip:
                    chapter = await self._get_chapter(following)
                    if (
                        chapter.work.id != current.work.id
                        or self._unit(chapter) == self._unit(current)
                        or self._unit(chapter) in flow.chapter_path
                    ):
                        raise SourceError("navigation_cycle", "章节后继关系异常，已保留当前正文。")
                    if not matches_filters(chapter.work, flow.filters):
                        raise SourceError("filter_mismatch", "下一章不符合当前硬筛选。")
                    flow.chapter_path.append(self._unit(chapter))
                    flow.predecessor_path = []
                    return _Prepared(chapter, flow, finished, "本作下一章")
                finished[current.work.id] = dict(
                    count=current.work.chapter_count, updated=current.work.updated
                )

            if current is not None and flow.series_url:
                if flow.series_queue is None:
                    page = await self._queue_page("series", flow.filters, flow.series_url, budget)
                    flow.series_queue = _Queue.from_page("series", flow.filters, page)
                while not flow.series_found:
                    item = await self._take(flow.series_queue, budget)
                    if item is None:
                        raise SourceError(
                            "parse_error", "系列中未找到当前作品，暂不能确定后继顺序。"
                        )
                    flow.series_found = item.id == current.work.id
                while True:
                    item = await self._take(flow.series_queue, budget)
                    if item is None:
                        break
                    try:
                        eligible = await self._eligible_work(
                            item, flow, finished, seen, include_read=flow.revisit_series
                        )
                    except _BudgetReached:
                        flow.series_queue.index -= 1
                        raise
                    if eligible:
                        chapter, detail = eligible
                        self._adopt_work(flow, chapter, detail, keep_series=True)
                        flow.revisit_series = False
                        note = "系列续读；保留硬筛选，关键词不限定续作。"
                        if len(flow.skipped) > skipped_before:
                            note += " 已跳过不匹配或需确认访问的作品。"
                        return _Prepared(chapter, flow, finished, "系列续读", note)
                flow.series_url = None
                flow.series_queue = None
                flow.series_found = False

            if flow.active_kind == "search" and flow.origin is not None:
                while True:
                    item = await self._take(flow.origin, budget)
                    if item is None:
                        transitioned = True
                        flow.active_kind = "recent"
                        break
                    try:
                        eligible = await self._eligible_work(item, flow, finished, seen)
                    except _BudgetReached:
                        flow.origin.index -= 1
                        raise
                    if eligible:
                        chapter, detail = eligible
                        self._adopt_work(flow, chapter, detail)
                        return _Prepared(chapter, flow, finished, "搜索结果")

            if flow.recent is None:
                page = await self._queue_page("recent", flow.filters, None, budget)
                flow.recent = _Queue.from_page("recent", flow.filters, page)
            flow.active_kind = "recent"
            while True:
                item = await self._take(flow.recent, budget)
                if item is None:
                    return _Prepared(
                        None,
                        flow,
                        finished,
                        notice="当前没有新的可读内容；可换条件、返回历史或明确重读缓存。",
                    )
                try:
                    eligible = await self._eligible_work(item, flow, finished, seen)
                except _BudgetReached:
                    flow.recent.index -= 1
                    raise
                if eligible:
                    chapter, detail = eligible
                    self._adopt_work(flow, chapter, detail)
                    notice = "搜索结果已浏览完毕，正在为您推荐近期新作" if transitioned else ""
                    if len(flow.skipped) > skipped_before:
                        notice += " 已跳过不匹配或需确认访问的作品。"
                    return _Prepared(chapter, flow, finished, "最近更新", notice)
        except _BudgetReached:
            return _Prepared(
                None,
                flow,
                finished,
                notice="本轮已达到候选检查上限，正文保留；再次下一条可继续检查。",
            )

    def _apply(self, prepared: _Prepared, epoch: int) -> Chapter | None:
        if epoch != self._epoch or self._closed:
            return None
        self._remember_current()
        self._flow = prepared.flow
        self._finished = dict(list(prepared.finished.items())[-512:])
        for name, limit in (
            ("skipped", 1024),
            ("chapter_path", 256),
            ("predecessor_path", 256),
            ("skipped_series", 64),
        ):
            setattr(self._flow, name, getattr(self._flow, name)[-limit:])
        self.notice = prepared.notice
        if prepared.chapter is None:
            # Proven duplicate/filter skips can be checkpointed after a bounded
            # scan; a source exception never reaches this commit point.
            self.save()
            return None
        self.current = prepared.chapter
        # Navigation consumes the front of the rolling window. An unrelated
        # discovery request must yield immediately so the newly missing chapter
        # can be prepared; keep all successfully cached chapters and successors.
        task = self._prefetch_task
        if (
            task and not task.done()
            and self._prefetch_focus not in (None, "next")
            and len(self._prefetch_ready) < 3
        ):
            self._prefetch_generation += 1
            self._prefetch_retired.add(task)
            task.add_done_callback(self._prefetch_retired.discard)
            task.cancel()
            self._prefetch_task = None
            self._prefetch_complete = False
            self._prefetch_changed.set()
        if self._prefetch_paused:
            # A successful explicit navigation starts a new preparation window.
            # Polling alone stays paused; the cancelled worker remains tracked
            # in retired tasks and its old generation cannot publish results.
            self._prefetch_paused = False
            self._prefetch_complete = False
            self._prefetch_task = None
        self._cache_chapter(self.current)
        self.position = {}
        self.source_label = prepared.label
        self._mark_seen(self.current)
        self._history = self._history[: self._history_index + 1]
        self._history.append(
            dict(
                url=self.current.url,
                position={},
                flow=asdict(self._flow),
                source_label=self.source_label,
            )
        )
        self._history = self._history[-100:]
        self._history_index = len(self._history) - 1
        self.save()
        return self.current

    async def _visit_history(self, target: int, epoch: int) -> Chapter | None:
        if not 0 <= target < len(self._history):
            self.notice = "已到本次阅读历史的边界。"
            return None
        entry = copy.deepcopy(self._history[target])
        chapter = await self._get_chapter(entry["url"])
        if epoch != self._epoch or self._closed:
            return None
        self._remember_current()
        self.current = chapter
        self._flow = _decode_flow(entry["flow"])
        self.position = entry["position"]
        self.source_label = entry.get("source_label", "阅读历史")
        self._history_index = target
        # The historical successor may differ from a speculative canonical
        # successor (for example after a skip). Keep cached bodies, but never
        # consume the previous location's navigation queue after this move.
        self._begin()
        self.notice = "已恢复阅读历史与位置。"
        self.save()
        return chapter

    async def previous(self) -> Chapter | None:
        if self.current is None:
            return None
        original_epoch = self._epoch
        with self._errors(original_epoch):
            # A cached canonical predecessor must not wait behind an unrelated
            # next-chapter HTTP request. This preview is forbidden to perform I/O.
            token = _cache_only.set(self)
            try:
                prepared = await self._resolve_previous(
                    self.current, copy.deepcopy(self._flow), _Budget()
                )
                resolved_locally = True
            except (_CacheMiss, _BudgetReached):
                prepared, resolved_locally = None, False
            finally:
                _cache_only.reset(token)
            if not resolved_locally:
                prepared = await self._wait_branch("previous", original_epoch)
        if original_epoch != self._epoch or self._closed:
            return None
        epoch, _ = self._begin()
        with self._errors(epoch):
            if prepared is None and not resolved_locally:
                try:
                    prepared = await self._resolve_previous(
                        self.current, copy.deepcopy(self._flow), _Budget()
                    )
                except _BudgetReached:
                    raise SourceError(
                        "scan_limit", "前序关系检查达到本轮上限，当前位置保留。"
                    ) from None
            if prepared is None:
                return await self._visit_history(self._history_index - 1, epoch)
            if (
                self._history_index > 0
                and self._history[self._history_index - 1]["url"] == prepared.chapter.url
            ):
                return await self._visit_history(self._history_index - 1, epoch)
            if epoch != self._epoch or self._closed:
                return None
            # Insert unread canonical context before this chapter. Advancing
            # returns to the original chapter's saved anchor rather than its top.
            self._remember_current()
            if self._history_index < 0:
                self._history = [
                    dict(
                        url=self.current.url,
                        position=copy.deepcopy(self.position),
                        flow=asdict(self._flow),
                        source_label=self.source_label,
                    )
                ]
                self._history_index = 0
            anchor = dict(
                paragraph=len(prepared.chapter.paragraphs) - 1,
                offset=len(prepared.chapter.paragraphs[-1]),
            )
            self._history.insert(
                self._history_index,
                dict(
                    url=prepared.chapter.url,
                    position=anchor,
                    flow=asdict(prepared.flow),
                    source_label=prepared.label,
                ),
            )
            if len(self._history) > 100:
                left = max(0, self._history_index - 50)
                self._history = self._history[left : left + 100]
                self._history_index -= left
            self.current, self._flow = prepared.chapter, prepared.flow
            self.position, self.source_label = anchor, prepared.label
            self._mark_seen(self.current)
            self.notice = prepared.notice
            self.save()
            return self.current

    async def _resolve_previous(self, current: Chapter, flow: _Flow, budget: _Budget):
        previous_url = current.previous_url
        if not previous_url:
            refs = flow.chapters
            if not refs and current.position > 1:
                detail = await self._get_work(current.work.url)
                refs = flow.chapters = copy.deepcopy(detail.chapters)
            for index, ref in enumerate(refs):
                if ref.id == current.id and index:
                    previous_url = refs[index - 1].url
                    break
        if previous_url:
            chapter = await self._get_chapter(previous_url)
            if (
                chapter.work.id != current.work.id
                or chapter.id == current.id
                or self._unit(chapter) in flow.predecessor_path
            ):
                raise SourceError("navigation_cycle", "前序章节关系异常，已保留当前正文。")
            if not matches_filters(chapter.work, flow.filters):
                raise SourceError("filter_mismatch", "前序章节不符合当前硬筛选。")
            flow.chapter_path = [self._unit(chapter)]
            flow.predecessor_path.append(self._unit(current))
            return _Prepared(chapter, flow, copy.deepcopy(self._finished), "本作上一章")
        if not flow.series_url:
            return None
        # Only explicit SeriesRef supplies story order. Collections, tags and
        # search order are not interpreted as narrative predecessors.
        url, visited, before, found = flow.series_url, set(), [], False
        while url:
            if url in visited:
                raise SourceError("pagination_cycle", "系列分页重复，已保留当前位置。")
            visited.add(url)
            page = await self._queue_page("series", flow.filters, url, budget)
            for work in page.items:
                budget.candidate()
                if work.id == current.work.id:
                    found = True
                    break
                before.append(work)
            if found:
                break
            url = page.next_url
        if not found:
            raise SourceError("parse_error", "系列中未找到当前作品，无法确定前序。")
        for summary in reversed(before):
            if not matches_filters(summary, flow.filters) or summary.rating in {
                "Mature",
                "Explicit",
            }:
                continue
            try:
                detail = await self._get_work(summary.url)
                if not matches_filters(detail.work, flow.filters):
                    continue
                last = detail.chapters[-1].url if detail.chapters else detail.chapter.url
                chapter = (
                    detail.chapter if last == detail.chapter.url else await self._get_chapter(last)
                )
                if not matches_filters(chapter.work, flow.filters):
                    continue
            except SourceError as exc:
                if exc.code in {"not_found", "adult_confirmation", "login_required"}:
                    continue
                raise
            self._adopt_work(flow, chapter, detail, keep_series=True)
            flow.series_queue, flow.series_found = None, False
            flow.revisit_series = True
            return _Prepared(
                chapter,
                flow,
                copy.deepcopy(self._finished),
                "系列前一作末章",
                "按所选系列回看前序；保留硬筛选。",
            )
        return None

    async def _wait_branch(self, name: str, epoch: int) -> _Prepared | None:
        while epoch == self._epoch and not self._closed:
            base = self._prefetch_base
            if (
                not base
                or not self.current
                or (
                    self._unit(base) != self._unit(self.current)
                    if name == "previous"
                    else base.work.id != self.current.work.id
                )
            ):
                return None
            prepared = self._previous_ready if name == "previous" else self._branch_ready.get(name)
            if prepared is not None:
                return copy.deepcopy(prepared)
            if (
                name == "skip_work" and not self._following_chapter(self.current, self._flow)
            ):
                # At the last chapter, next and skip-work have the same
                # successor. Share that ready/in-flight item before considering
                # cancellation of unrelated deep read-ahead.
                return await self._consume_prefetch(epoch)
            if self._prefetch_paused:
                return None
            if name in self._branch_errors:
                self._branch_done.discard(name)
                raise self._remaining_error(self._branch_errors.pop(name))
            if name in self._branch_done or not self._prefetch_task or self._prefetch_task.done():
                return None
            if self._prefetch_focus != name:
                # An explicit skip/previous changes the reading path. Its
                # caller retires unrelated speculation via _begin(); only a
                # matching in-flight branch is worth waiting for and sharing.
                return None
            promote = getattr(self.source, "promote", None)
            if promote:
                promote(self._prefetch_task)
            self._prefetch_changed.clear()
            await self._prefetch_changed.wait()

    async def next(self) -> Chapter | None:
        return await self._advance()

    async def skip(self) -> Chapter | None:
        """Compatibility action in an error view; explicit work skip, never consent."""
        return await self.skip_work()

    async def skip_work(self) -> Chapter | None:
        """Skip remaining chapters while retaining the selected series path."""
        return await self._advance(skip=True)

    async def _consume_prefetch(self, epoch: int) -> _Prepared | None:
        while epoch == self._epoch and not self._closed:
            if self._prefetch_ready:
                return self._prefetch_ready.pop(0)
            if self._prefetch_paused:
                return None
            if self._prefetch_error:
                error, self._prefetch_error = self._prefetch_error, None
                self._prefetch_error_after_work = None
                if self._prefetch_task and self._prefetch_task.done():
                    self._prefetch_task = None
                raise self._remaining_error(error)
            if self._prefetch_terminal:
                prepared, self._prefetch_terminal = self._prefetch_terminal, None
                if self._prefetch_task and self._prefetch_task.done():
                    self._prefetch_task = None
                return prepared
            task = self._prefetch_task
            if task is None or task.done():
                return None
            if self._prefetch_focus != "next":
                # An unrelated skip/previous branch must not own a foreground
                # next operation. The source still serializes actual HTTP; an
                # identical in-flight fetch is shared by _source_call.
                return None
            promote = getattr(self.source, "promote", None)
            if promote:
                promote(task)
            changed = self._prefetch_changed
            changed.clear()
            await changed.wait()
        return None

    async def _advance(self, *, skip=False) -> Chapter | None:
        if self._advancing and self._advancing_epoch == self._epoch:
            return None
        branch = None
        if skip:
            old_epoch = self._epoch
            with self._errors(old_epoch):
                branch = await self._wait_branch("skip_work", old_epoch)
            if old_epoch != self._epoch or self._closed:
                return None
            epoch, _ = self._begin()
        else:
            # Advancing along the existing path consumes a queued/pending item.
            # It must not cancel that request or discard its two ready successors.
            epoch = self._epoch
            self.pending_url = None
        self._advancing = True
        self._advancing_epoch = epoch
        try:
            if not skip and self._history_index + 1 < len(self._history):
                return await self._visit_history(self._history_index + 1, epoch)
            prepared = branch if skip else await self._consume_prefetch(epoch)
            if epoch != self._epoch or self._closed:
                return None
            if prepared is None:
                flow = copy.deepcopy(self._flow)
                resolver = (epoch, asyncio.current_task())
                self._foreground_resolver = resolver
                self._foreground_settled.clear()
                try:
                    prepared = await self._resolve(
                        self.current, flow, copy.deepcopy(self._finished), set(self._seen), skip=skip
                    )
                finally:
                    if self._foreground_resolver == resolver:
                        self._foreground_resolver = None
                        self._foreground_settled.set()
            return self._apply(prepared, epoch)
        except SourceError as exc:
            if epoch != self._epoch or self._closed:
                return None
            self.pending_url = getattr(exc, "url", None)
            raise
        finally:
            if self._advancing_epoch == epoch:
                self._advancing = False

    async def _fill_prefetch(self, epoch: int, changed: asyncio.Event):
        base = self.current
        same_base = self._prefetch_base and self._unit(base) == self._unit(self._prefetch_base)
        if not same_base:
            self._previous_ready = None
            self._branch_ready.clear()
            self._branch_roots.clear()
            self._branch_units.clear()
            self._branch_errors.clear()
            self._branch_done.clear()
        if self._prefetch_error and self._prefetch_error_after_work == base.work.id:
            # A new chapter in the same work does not change the failed
            # next-work lookup. Derive this branch error from the durable
            # forward failure rather than the previous window's branch map.
            self._branch_errors["skip_work"] = self._prefetch_error
        self._prefetch_budget_limited = False
        self._prefetch_complete = False
        self._prefetch_retry_attempt = 0
        self._prefetch_base = base
        self._prefetch_started = time.monotonic()
        budget = _Budget(request_limit=24, page_limit=6, candidate_limit=40)
        generation = self._prefetch_generation
        context = _speculation.set((self, epoch, budget, generation))
        base_flow = copy.deepcopy(self._flow)
        base_finished = copy.deepcopy(self._finished)
        base_seen = set(self._seen)
        next_failed = bool(self._prefetch_error)

        def active():
            return (
                epoch == self._epoch
                and not self._closed
                and generation == self._prefetch_generation
            )

        async def forward_one():
            nonlocal next_failed
            while self._foreground_resolver and self._foreground_resolver[0] == epoch:
                remaining = self.PREFETCH_SECONDS - (time.monotonic() - budget.started)
                try:
                    await asyncio.wait_for(self._foreground_settled.wait(), max(0, remaining))
                except TimeoutError:
                    if active():
                        self._prefetch_budget_limited = True
                    raise _PreparationPaused from None
            if not active():
                return
            self._prefetch_focus = "next"
            changed.set()
            if next_failed or self._prefetch_terminal or len(self._prefetch_ready) >= 3:
                return
            last = self._prefetch_ready[-1] if self._prefetch_ready else None
            current = last.chapter if last else self.current
            flow = copy.deepcopy(last.flow if last else self._flow)
            finished = copy.deepcopy(last.finished if last else self._finished)
            seen = set(self._seen) | {self._unit(p.chapter) for p in self._prefetch_ready}
            try:
                prepared = await self._resolve(current, flow, finished, seen, budget=budget)
                if not active():
                    return
                if prepared.chapter:
                    self._prefetch_ready.append(prepared)
                else:
                    self._prefetch_terminal = prepared
                    self._prefetch_budget_limited |= "上限" in prepared.notice
            except SourceError as exc:
                if active():
                    next_failed = True
                    self._prefetch_error = exc
                    self._prefetch_error_after_work = (
                        current.work.id if current and not self._following_chapter(current, flow) else None
                    )
                    if self._prefetch_error_after_work == base.work.id:
                        # The failed next-work lookup also belongs to skip-work.
                        # Reuse its Retry-After deadline instead of issuing the
                        # same request and restarting the cooldown later.
                        self._branch_errors["skip_work"] = exc
            changed.set()

        def remember(prepared):
            if prepared and prepared.chapter and prepared.chapter.work.id != base.work.id:
                ident = prepared.chapter.work.id
                if ident not in self._branch_units and len(self._branch_roots) < 3:
                    self._branch_roots.append(prepared)
                    self._branch_units[ident] = [prepared.chapter]

        async def branch_head(name, current, flow, finished):
            self._prefetch_focus = name
            changed.set()
            if name in self._branch_ready:
                return self._branch_ready[name]
            if name in self._branch_done or name in self._branch_errors:
                return None
            try:
                prepared = await self._resolve(
                    current, flow, finished, base_seen, skip=True, budget=budget
                )
                if not active():
                    return None
                if prepared.chapter:
                    self._branch_ready[name] = prepared
                    remember(prepared)
                    return prepared
                self._prefetch_budget_limited |= "上限" in prepared.notice
            except SourceError as exc:
                if active():
                    self._branch_errors[name] = exc
            finally:
                if active():
                    self._branch_done.add(name)
                    changed.set()
            return None

        try:
            # Continuous chapter reading has priority over skip/discovery
            # branches. Publish each successor as soon as it arrives.
            for _ in range(3):
                await forward_one()
            if not active():
                return
            checked_flow = (
                self._prefetch_ready[0].flow
                if self._prefetch_ready
                else self._prefetch_terminal.flow
                if self._prefetch_terminal
                else None
            )
            if checked_flow:
                base_flow.skipped = list(set(base_flow.skipped) | set(checked_flow.skipped))
            if "previous" not in self._branch_done:
                self._prefetch_focus = "previous"
                changed.set()
                try:
                    previous = await self._resolve_previous(base, copy.deepcopy(base_flow), budget)
                    if active():
                        self._previous_ready = previous
                except SourceError as exc:
                    if active():
                        self._branch_errors["previous"] = exc
            if not active():
                return
            self._branch_done.add("previous")
            changed.set()
            normal = await branch_head(
                "skip_work", base, copy.deepcopy(base_flow), copy.deepcopy(base_finished)
            )
            # Follow the natural series/search order to prepare distinct works.
            for index in range(3):
                if not active() or not normal or len(self._branch_roots) >= 3:
                    break
                normal = await branch_head(
                    "work_" + str(index + 2),
                    normal.chapter,
                    copy.deepcopy(normal.flow),
                    copy.deepcopy(normal.finished),
                )
            # A foreground consumer may have used the initial ready chapter
            # while branch heads loaded. Refill up to the same three-unit cap.
            for _ in range(3):
                if active():
                    await forward_one()
            # Two additional chapters per work, never the full book. Round-robin
            # depth avoids spending the entire remainder on one large work.
            for _ in range(2):
                for root in self._branch_roots:
                    if not active():
                        return
                    self._prefetch_focus = "work:" + root.chapter.work.id
                    changed.set()
                    units = self._branch_units[root.chapter.work.id]
                    last = units[-1]
                    if "work:" + last.work.id in self._branch_errors:
                        continue
                    following = last.next_url
                    if not following:
                        for index, ref in enumerate(root.flow.chapters[:-1]):
                            if ref.id == last.id:
                                following = root.flow.chapters[index + 1].url
                                break
                    if not following or len(units) >= 3:
                        continue
                    try:
                        chapter = await self._get_chapter(following)
                        if chapter.work.id != last.work.id or any(
                            c.id == chapter.id for c in units
                        ):
                            raise SourceError(
                                "navigation_cycle", "预取章节关系异常；已保留当前正文。"
                            )
                        if not matches_filters(chapter.work, root.flow.filters):
                            raise SourceError("filter_mismatch", "预取章节不符合硬筛选。")
                        if active():
                            units.append(chapter)
                    except SourceError as exc:
                        if active():
                            self._branch_errors["work:" + last.work.id] = exc
                    changed.set()
        except _BudgetReached:
            if active():
                self._prefetch_budget_limited = True
        except _PreparationPaused:
            pass
        except asyncio.CancelledError:
            pass
        finally:
            if active():
                self._prefetch_complete = True
                self._prefetch_focus = None
            _speculation.reset(context)
            changed.set()

    async def prefetch(self) -> Chapter | None:
        epoch = self._epoch
        while epoch == self._epoch and not self._closed and self.current is not None:
            if self._prefetch_paused or (self._advancing and self._advancing_epoch == epoch):
                break
            if (
                self._prefetch_complete
                and self._prefetch_base
                and self._unit(self._prefetch_base) == self._unit(self.current)
            ):
                break
            if self._prefetch_task is None or self._prefetch_task.done():
                self._prefetch_epoch = epoch
                self._prefetch_task = asyncio.create_task(
                    self._fill_prefetch(epoch, self._prefetch_changed)
                )
            task = self._prefetch_task
            try:
                # A retired source may deliver its cancellation late. Wake on
                # replacement as well as completion so it cannot prevent the
                # new chapter's worker from starting. wait() never cancels the
                # owned worker when this outer waiter is cancelled.
                while task is self._prefetch_task and not task.done() and epoch == self._epoch:
                    changed = self._prefetch_changed
                    changed.clear()
                    wake = asyncio.create_task(changed.wait())
                    try:
                        await asyncio.wait((task, wake), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        wake.cancel()
                        await asyncio.gather(wake, return_exceptions=True)
                if task.done():
                    await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    return None
            # A chapter consumed during preparation may retire a skip worker
            # or outgrow its completed window. Follow that new position now;
            # the UI should not need another keypress to start replenishment.
        return self._prefetch_ready[0].chapter if self._prefetch_ready else None

    async def retry_prefetch(self) -> Chapter | None:
        """Explicit retry of failed preparation; ready units and reader stay put."""
        if self._prefetch_paused:
            self._prefetch_paused = False
            self._prefetch_task = None
        if self._prefetch_task and not self._prefetch_task.done():
            return await self.prefetch()
        if self._prefetch_cooldown_until > time.monotonic():
            return self._prefetch_ready[0].chapter if self._prefetch_ready else None
        self._branch_done.difference_update(self._branch_errors)
        self._branch_errors.clear()
        self._prefetch_error = None
        self._prefetch_error_after_work = None
        if self._prefetch_budget_limited:
            self._prefetch_terminal = None
        self._prefetch_complete = False
        return await self.prefetch()

    def pause_prefetch(self):
        """Pause this preparation cycle without discarding successfully ready units."""
        self._prefetch_generation += 1
        self._prefetch_paused = True
        self._prefetch_complete = True
        self._prefetch_retry_at = 0.0
        task = self._prefetch_task
        if task and not task.done():
            self._prefetch_retired.add(task)
            task.add_done_callback(self._prefetch_retired.discard)
            task.cancel()
        self._prefetch_changed.set()

    def cancel_foreground(self):
        """Invalidate a cancelled view's late response without changing committed state."""
        self._epoch += 1
        self.pending_url = None
        self.pause_prefetch()

    def clear_prepared_cache(self) -> dict:
        """Clear prepared units and disk cache while retaining resume/search state."""
        self.restore_results()
        self._begin()
        self._prefetch_paused = True
        self._prefetch_complete = True
        saved = self.save()
        try:
            compact = getattr(self.store, "compact_on_exit", None)
            if compact:
                # No state argument: failed save must not block VACUUM or
                # replace the last valid session with an oversized snapshot.
                compact()
            else:
                self.store.clear_cache()
            info = self.store.cache_info() if hasattr(self.store, "cache_info") else {}
        except StorageError as exc:
            self.storage_notice = " ".join(filter(None, (
                self.storage_notice,
                f"临时缓存清理或磁盘回收未能完成：{exc}。当前位置已保留。",
            )))
            return dict(
                cleared=False, paused=True, saved=saved, storage_notice=self.storage_notice
            )
        return dict(
            info, cleared=True, paused=True, saved=saved, storage_notice=self.storage_notice
        )

    async def close(self):
        if self._closed:
            return
        pending = self._prefetch_task
        self._begin()
        waiting = self._prefetch_retired | ({pending} if pending else set())
        if waiting:
            await asyncio.gather(*waiting, return_exceptions=True)
        self.save()
        self._closed = True
        try:
            await self.source.aclose()
        finally:
            clear_cache = getattr(self.store, "clear_cache", None)
            if clear_cache:
                clear_cache()
