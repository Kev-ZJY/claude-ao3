"""Bounded anonymous AO3 adapter with explicit, per-work content confirmation."""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
import unicodedata
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

from .models import (
    CATEGORY_IDS,
    CATEGORY_OPTIONS,
    RATING_IDS,
    RATING_OPTIONS,
    WARNING_IDS,
    WARNING_OPTIONS,
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
from .network_diagnostics import classify_transport_error
from .curl_transport import CurlTransport
import shutil


class SourceError(Exception):
    """A user-visible state; messages deliberately omit transport/proxy credentials."""

    def __init__(
        self,
        code: str,
        message: str,
        retry_after: float | None = None,
        url: str | None = None,
        *,
        failure_kind: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
        attempts: int = 0,
        curl_exit_code: int | None = None,
        confirmation_url: str | None = None,
    ):
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.url = url
        self.failure_kind = failure_kind or code
        self.http_status = http_status
        self.retryable = (
            code in {"network", "unavailable", "rate_limited"} if retryable is None else retryable
        )
        self.attempts = attempts
        self.curl_exit_code = curl_exit_code
        self.confirmation_url = confirmation_url
        super().__init__(message)


def _clean(text: str) -> str:
    # No remote text may emit terminal escape/control sequences, including bidi controls.
    return "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))


def _text(node: Tag | None) -> str:
    return _clean(node.get_text(" ", strip=True)) if node else ""


def _integer(text: str) -> int | None:
    clean = text.strip().replace(",", "")
    return int(clean) if clean.isdigit() else None


class _RequestGate:
    """One in-flight operation; foreground waiters precede background waiters."""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._busy = False
        self._sequence = 0
        self._waiters = []

    @asynccontextmanager
    async def slot(self, priority: str):
        async with self._condition:
            self._sequence += 1
            waiter = [priority != "foreground", self._sequence, asyncio.current_task()]
            self._waiters.append(waiter)
            try:
                await self._condition.wait_for(
                    lambda: (
                        not self._busy and min(self._waiters, key=lambda item: item[:2]) is waiter
                    )
                )
            except BaseException:
                self._waiters.remove(waiter)
                self._condition.notify_all()
                raise
            self._waiters.remove(waiter)
            self._busy = True
        try:
            yield
        finally:
            async with self._condition:
                self._busy = False
                self._condition.notify_all()

    def promote(self, task: asyncio.Task) -> bool:
        for waiter in self._waiters:
            if waiter[2] is task:
                waiter[0] = False
                return True
        # An already-issued request continues; promotion never duplicates it.
        return False


class AO3Source:
    def __init__(
        self,
        base_url: str = "https://archiveofourown.org",
        *,
        timeout: float = 20,
        min_interval: float = 0.75,
        cache_ttl: float = 300,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 1,
        retry_delay: float = 1,
        network: str = "system",
        http_client: str = "auto",
    ):
        if network not in {"system", "direct"}:
            raise SourceError("invalid_network", "网络方式须为 system 或 direct。")
        self.network = network
        if http_client not in {"auto", "curl", "httpx"}:
            raise SourceError("invalid_transport", "请求方式须为 auto、curl 或 httpx。")
        self.transport_name = "custom" if client is not None or transport is not None else "httpx/http1.1"
        base = urlsplit(base_url)
        if (
            base.scheme != "https"
            or not base.hostname
            or base.username
            or base.password
            or base.query
            or base.fragment
            or base.path not in {"", "/"}
        ):
            raise SourceError("invalid_url", "书源须为不含账号和路径的 HTTPS 域名。")
        self.base_url = urlunsplit(("https", base.netloc.lower(), "", "", ""))
        self._origin = (base.hostname.lower(), base.port or 443)
        self.min_interval = max(0.0, min_interval)
        self.cache_ttl = max(0.0, cache_ttl)
        # Callers with an outer retry/deadline policy set this to zero. There
        # is never a hidden HTTP transport retry in addition to this one.
        self.max_retries = min(1, max(0, max_retries))
        self.retry_delay = max(0.0, retry_delay)
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 10), pool=min(timeout, 3))
        self._gate = _RequestGate()
        self._priority = ContextVar(f"source_priority_{id(self)}", default="foreground")
        self._last_request = 0.0
        self._blocked_until = 0.0
        self._blocked_status = 429
        self._blocked_kind = "http_429"
        self._circuit_until = 0.0
        self._network_failures = 0
        self._health = {
            "requests": 0,
            "retries": 0,
            "cache_hits": 0,
            "last_error": None,
            "last_latency_ms": None,
            "last_failure_kind": None,
            "last_http_status": None,
        }
        self._activities: dict[asyncio.Task, dict] = {}
        self._cache: OrderedDict[str, tuple[float, str, str]] = OrderedDict()
        self._content_warnings: OrderedDict[str, None] = OrderedDict()
        self._confirmed_works: set[str] = set()
        self._closed = False
        # Select once, before any network call. Never rotate clients or sources
        # to retry access-denied pages. Preserve the user's current proxy policy.
        if client is None and transport is None and http_client != "httpx":
            if shutil.which("curl"):
                transport = CurlTransport(network=network, http_version="1.1")
                self.transport_name = "curl/http1.1"
            elif http_client == "curl":
                raise SourceError("invalid_transport", "未找到 curl；可安装系统 curl 或使用 --http-client httpx。")
        try:
            self._client = client or httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=transport,
                headers={
                    "User-Agent": "Claude-AO3/0.6.0 (independent anonymous reading client)",
                    "Accept": "text/html",
                },
                trust_env=network == "system" and transport is None,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )
        except ImportError as exc:
            raise SourceError(
                "network",
                "代理依赖缺失，请重新安装包含 SOCKS 支持的 httpx[socks]。",
                failure_kind="proxy_dependency",
                retryable=False,
            ) from exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        self._closed = True
        self._cache.clear()
        self._content_warnings.clear()
        self._confirmed_works.clear()
        await self._client.aclose()

    close = aclose

    @asynccontextmanager
    async def request_context(self, *, priority: str = "foreground"):
        """Label this task's requests without preempting an HTTP already sent."""
        if priority not in {"foreground", "background"}:
            raise ValueError("Request priority must be foreground or background.")
        token = self._priority.set(priority)
        try:
            yield
        finally:
            self._priority.reset(token)

    def promote(self, task: asyncio.Task) -> bool:
        """Promote a queued task. False also means it may already be in flight."""
        return self._gate.promote(task)

    @property
    def health(self) -> dict:
        """Metadata only; latency includes queueing and bounded retry waits."""
        now = time.monotonic()
        remaining = max(0.0, self._blocked_until - now, self._circuit_until - now)
        activity = min(
            self._activities.values(),
            key=lambda item: (item["priority"] != "foreground", item["started"]),
            default=None,
        )
        return {
            **self._health,
            "network": self.network,
            "transport": self.transport_name,
            "state": activity["state"] if activity else ("cooldown" if remaining else "idle"),
            "queued_requests": sum(item["state"] == "queued" for item in self._activities.values()),
            "retry_remaining_seconds": max(remaining, (activity or {}).get("until", now) - now),
            "consecutive_network_failures": self._network_failures,
            "circuit_remaining_seconds": max(0.0, self._circuit_until - now),
        }

    def _state(self, state: str, *, until: float = 0):
        activity = self._activities.get(asyncio.current_task())
        if activity is not None:
            activity.update(state=state, until=until)

    async def _retry_pause(self, attempt: int):
        self._health["retries"] += 1
        delay = min(2.0, self.retry_delay * attempt)
        delay *= random.uniform(0.8, 1.2)
        self._state("retry_wait", until=time.monotonic() + delay)
        await asyncio.sleep(delay)

    def _network_error(self, error: SourceError) -> SourceError:
        self._network_failures += 1
        if self._network_failures >= 2:
            # Two exhausted operations, not two individual attempts. A short
            # open circuit prevents background discovery from hammering a failure.
            self._circuit_until = time.monotonic() + 10
            error.retry_after = max(error.retry_after or 0, 10.0)
        return error

    @contextmanager
    def _parsing(self, url: str):
        try:
            yield
        except SourceError as error:
            # A retry must be able to recover from a partial/changing HTML response.
            self._cache.pop(url, None)
            self._health["last_error"] = error.code
            self._health["last_failure_kind"] = error.failure_kind
            if error.code != "invalid_url" and error.url is None:
                error.url = url
            raise

    def normalize_url(
        self, value: str, *, from_url: str | None = None, strip_consent: bool = False
    ) -> str:
        """Only anonymous read routes on the chosen origin. Never transmit userinfo."""
        if (
            not isinstance(value, str)
            or not value
            or "\\" in value
            or any(ord(c) < 32 for c in value)
        ):
            raise SourceError("invalid_url", "作品链接格式无效。")
        try:
            parsed = urlsplit(urljoin(from_url or self.base_url + "/", value))
            origin = (parsed.hostname.lower() if parsed.hostname else "", parsed.port or 443)
        except ValueError as exc:
            raise SourceError("invalid_url", "作品链接格式无效。") from exc
        if parsed.scheme != "https" or parsed.username or parsed.password or origin != self._origin:
            raise SourceError("invalid_url", "链接不属于当前书源，已停止跨站请求。")
        if parsed.path.startswith(("/users/login", "/users/sign_in")):
            raise SourceError("login_required", "此内容要求登录；当前版本仅访问匿名公开作品。")
        if not re.fullmatch(
            r"/(?:works(?:/search|/\d+(?:/chapters/\d+)?)?|series/\d+)/?", parsed.path
        ):
            raise SourceError("invalid_url", "链接不是支持的作品、章节、搜索或系列入口。")
        pairs = []
        for key, val in parse_qsl(parsed.query, keep_blank_values=True):
            if key == "view_adult":
                if (
                    not strip_consent and val.lower() not in {"false", "0", ""}
                    and self._work_key(parsed.path) not in self._confirmed_works
                ):
                    raise SourceError(
                        "adult_confirmation",
                        "请先打开不带确认参数的作品链接，阅读内容提示后再选择是否继续。",
                    )
                continue
            if key == "view_full_work":
                # Reading a work uses its first visible chapter, never the full-book endpoint.
                continue
            if key == "page" or key.startswith("work_search[") or key in {"commit", "utf8"}:
                pairs.append((key, val))
        return urlunsplit(
            (
                "https",
                urlsplit(self.base_url).netloc,
                parsed.path.rstrip("/") or "/",
                urlencode(pairs),
                "",
            )
        )

    def _link(self, href: str | None, url: str) -> str | None:
        if not href:
            return None
        try:
            return self.normalize_url(href, from_url=url, strip_consent=True)
        except SourceError:
            return None

    @staticmethod
    def _work_key(url: str) -> str | None:
        match = re.fullmatch(r"/works/(\d+)(?:/chapters/\d+)?", urlsplit(url).path)
        return match[1] if match else None

    def confirm_content_warning(self, url: str) -> None:
        """Called only after the reader explicitly confirms an observed notice."""
        url = self.normalize_url(url)
        work = self._work_key(url)
        if self._closed or not work or url not in self._content_warnings:
            raise SourceError("invalid_confirmation", "内容提示已失效，请重新打开目标作品。")
        self._confirmed_works.add(work)
        self._content_warnings.pop(url, None)

    def _request_url(self, url: str) -> str:
        # Keep consent out of chapter identities, navigation and saved state.
        if self._work_key(url) not in self._confirmed_works:
            return url
        parsed = urlsplit(url)
        pairs = parse_qsl(parsed.query) + [("view_adult", "true")]
        return urlunsplit(parsed._replace(query=urlencode(pairs)))

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            seconds = float(value)
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except ValueError:
            try:
                return max(
                    0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                )
            except (ValueError, TypeError, OverflowError):
                return None

    async def _get(self, url: str) -> tuple[BeautifulSoup, str]:
        url = self.normalize_url(url)
        start = time.monotonic()
        task = asyncio.current_task()
        self._activities[task] = {
            "state": "queued",
            "priority": self._priority.get(),
            "started": start,
            "until": 0,
        }
        try:
            result = await self._get_validated(url)
            self._health.update(last_error=None, last_failure_kind=None)
            return result
        except SourceError as error:
            self._health.update(last_error=error.code, last_failure_kind=error.failure_kind)
            if error.code != "invalid_url" and error.url is None:
                error.url = url
            raise
        finally:
            self._activities.pop(task, None)
            self._health["last_latency_ms"] = round((time.monotonic() - start) * 1000, 1)

    async def _get_validated(self, url: str) -> tuple[BeautifulSoup, str]:
        current, redirects, retries = url, 0, 0
        while True:
            self._state("queued")
            pending_error = None
            # Each attempt reacquires priority; retry sleep owns neither stream nor gate.
            async with self._gate.slot(self._priority.get()):
                if self._closed:
                    raise SourceError("network", "书源连接已关闭。", retryable=False)
                cached = self._cache.get(url)
                if cached and cached[0] > time.monotonic():
                    self._cache.move_to_end(url)
                    self._health["cache_hits"] += 1
                    return BeautifulSoup(cached[1], "html.parser"), cached[2]
                if self._blocked_until > time.monotonic():
                    wait = self._blocked_until - time.monotonic()
                    raise SourceError(
                        "rate_limited" if self._blocked_status == 429 else "network",
                        "来源要求等待，请在冷却结束后重试。",
                        wait,
                        failure_kind=self._blocked_kind,
                        http_status=self._blocked_status,
                    )
                if self._circuit_until > time.monotonic():
                    wait = self._circuit_until - time.monotonic()
                    raise SourceError(
                        "network",
                        "来源连续连接失败，短暂冷却后可重试。",
                        wait,
                        failure_kind="circuit_open",
                    )
                delay = self.min_interval - (time.monotonic() - self._last_request)
                if delay > 0:
                    self._state("interval_wait", until=time.monotonic() + delay)
                    await asyncio.sleep(delay)
                self._last_request = time.monotonic()
                self._health["requests"] += 1
                self._health["last_http_status"] = None
                self._state("requesting")
                try:
                    # AO3's cookie grants consent site-wide. Retain our narrower
                    # per-work decision instead, including across redirects.
                    for cookie in list(self._client.cookies.jar):
                        if cookie.name == "view_adult":
                            self._client.cookies.delete(cookie.name, cookie.domain, cookie.path)
                    async with self._client.stream(
                        "GET", self._request_url(current), follow_redirects=False, timeout=self._timeout
                    ) as response:
                        status = response.status_code
                        self._health["last_http_status"] = status
                        self._state("reading")
                        if status in {301, 302, 303, 307, 308}:
                            redirects += 1
                            if redirects > 4 or not response.headers.get("location"):
                                raise SourceError(
                                    "network",
                                    "来源重定向异常，请稍后重试。",
                                    failure_kind="redirect_error",
                                    http_status=status,
                                    retryable=False,
                                )
                            current = self.normalize_url(
                                response.headers["location"], from_url=current
                            )
                            continue
                        if status == 429 or status == 503:
                            after = self._retry_after(response.headers.get("retry-after"))
                            if status == 429 and after is None:
                                after = 60.0
                            if after is not None:
                                self._blocked_until = time.monotonic() + after
                                self._blocked_status, self._blocked_kind = status, f"http_{status}"
                                raise SourceError(
                                    "rate_limited" if status == 429 else "network",
                                    f"来源要求稍后重试（HTTP {status}），当前内容已保留。",
                                    after,
                                    failure_kind=f"http_{status}",
                                    http_status=status,
                                )
                        if status == 401:
                            raise SourceError(
                                "login_required",
                                "来源要求登录，当前版本仅访问匿名公开作品。",
                                failure_kind="http_401",
                                http_status=status,
                            )
                        if status == 403:
                            raise SourceError(
                                "forbidden",
                                "来源拒绝匿名访问（HTTP 403）；未尝试绕过限制。",
                                failure_kind="http_403",
                                http_status=status,
                            )
                        if status in {404, 410}:
                            raise SourceError(
                                "not_found",
                                "作品不存在、已删除，或当前无法公开访问。",
                                failure_kind=f"http_{status}",
                                http_status=status,
                            )
                        if status >= 500 or status == 408:
                            raise SourceError(
                                "network",
                                f"来源服务暂时失败（HTTP {status}），当前内容已保留。",
                                failure_kind=f"http_{status}",
                                http_status=status,
                            )
                        if status != 200:
                            raise SourceError(
                                "network",
                                f"来源返回 HTTP {status}，请检查后重试。",
                                failure_kind=f"http_{status}",
                                http_status=status,
                                retryable=False,
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 4_000_000:
                                raise SourceError(
                                    "parse_error", "页面过大，已停止读取；请在原站查看。"
                                )
                        encoding = response.encoding or "utf-8"
                        try:
                            html = body.decode(encoding, errors="replace")
                        except LookupError:
                            html = body.decode("utf-8", errors="replace")
                    self._network_failures = 0
                    self._circuit_until = 0.0
                    soup = BeautifulSoup(html, "html.parser")
                    self._check_page(soup, current)
                    self._cache[url] = (time.monotonic() + self.cache_ttl, html, current)
                    self._cache.move_to_end(url)
                    while len(self._cache) > 32:
                        self._cache.popitem(last=False)
                    return soup, current
                except httpx.TransportError as exc:
                    kind = getattr(exc, "failure_kind", None) or classify_transport_error(exc)
                    descriptions = {
                        "connect_timeout": "建立连接超时",
                        "read_timeout": "等待响应或读取数据超时",
                        "write_timeout": "发送请求超时",
                        "pool_timeout": "等待连接池超时",
                        "proxy_error": "代理连接失败",
                        "connect_error": "建立连接失败",
                        "timeout": "请求超时，已达到等待上限",
                        "response_too_large": "页面超过本地读取上限，已停止下载",
                        "dns_error": "无法解析书源域名",
                        "proxy_dns_error": "无法解析已配置的代理地址，请检查代理配置",
                        "tls_handshake_error": "与目标建立 TLS 连接失败",
                        "tls_certificate_error": "TLS 证书校验失败，已停止自动重试；请检查证书和网络配置",
                        "tls_ca_error": "本机 CA 证书配置不可用，请修复后重试",
                    }
                    pending_error = SourceError(
                        "network",
                        descriptions.get(kind, "网络传输失败") + "；当前内容已保留。",
                        failure_kind=kind,
                        attempts=retries + 1,
                        curl_exit_code=getattr(exc, "curl_exit_code", None),
                        retryable=getattr(exc, "retryable", kind not in {"local_protocol_error", "unsupported_protocol"}),
                    )
                except SourceError as error:
                    # A work -> chapter redirect is another HTTP request, not
                    # another recovery attempt. Otherwise normal AO3 redirects
                    # prematurely exhaust the App's three-attempt budget.
                    error.attempts = retries + 1
                    pending_error = error
            # The response and priority gate have both exited before any retry decision.
            self._health.update(
                last_error=pending_error.code,
                last_failure_kind=pending_error.failure_kind,
            )
            if (
                not pending_error.retryable
                or pending_error.code == "rate_limited"
                or (pending_error.retry_after is not None and pending_error.retry_after > 0)
            ):
                raise pending_error
            if retries < self.max_retries:
                retries += 1
                await self._retry_pause(retries)
                continue
            raise self._network_error(pending_error)

    def _check_page(self, soup: BeautifulSoup, url: str):
        main = soup.select_one("#main") or soup
        if soup.select_one("#challenge-form, #cf-challenge-running, .g-recaptcha") or _text(
            soup.title
        ).lower().startswith("just a moment"):
            raise SourceError("forbidden", "来源要求访问验证，程序已停止自动请求。")
        if main.select_one(
            'form[action*="/users/login"] input[type="password"], form[action*="/users/sign_in"] input[type="password"]'
        ):
            raise SourceError("login_required", "此作品仅向登录用户开放，当前版本不登录。")
        if not main.select_one("#chapters") and (
            main.select_one(".adult") or (
                main.select_one("p.caution")
                and main.select_one('a[href*="view_adult=true"]')
            )
        ):
            confirmation_url = None
            for link in main.select('a[href*="view_adult=true"]'):
                target = self._link(link.get("href"), url)
                if (
                    target and self._work_key(url)
                    and self._work_key(target) == self._work_key(url)
                    and ("view_adult", "true") in parse_qsl(urlsplit(link["href"]).query)
                ):
                    confirmation_url = url
                    self._content_warnings[url] = None
                    self._content_warnings.move_to_end(url)
                    while len(self._content_warnings) > 32:
                        self._content_warnings.popitem(last=False)
                    break
            raise SourceError(
                "adult_confirmation", "此作品可能包含成人内容，请阅读提示后选择是否继续。",
                url=url, confirmation_url=confirmation_url,
            )
        if not main.select_one("#workskin, li.work.blurb, ol.work.index, .series"):
            text = _text(main).lower()
            if (
                "work has been deleted" in text
                or "couldn't find the work" in text
                or "work you were looking for doesn't exist" in text
            ):
                raise SourceError("not_found", "作品已删除或不存在。")

    @staticmethod
    def _ids(values, options: dict[str, str], reverse: dict[str, str]) -> list[str]:
        result = []
        for value in values:
            ident = reverse.get(str(value), str(value))
            if ident not in options:
                raise SourceError("invalid_filter", "筛选项不是当前支持的 AO3 标签。")
            if ident not in result:
                result.append(ident)
        return result

    async def recent(
        self,
        page_url: str | None = None,
        *,
        warnings=(),
        categories=(),
        rating: str = "",
        language: str = "",
    ) -> Page:
        """Bootstrap unfiltered/language-only reading from the finite latest index.

        Its next_url is an explicit transition to sorted Work Search page one,
        not a second page of /works or a complete language-specific list. Unknown
        language metadata never passes the local language filter. An empty sample
        still has the search bridge. Additional hard filters and subsequent pages
        retain the search contract; the engine deduplicates overlapping works.
        """
        if not page_url and not any((warnings, categories, rating)):
            url = self.base_url + "/works"
            soup, final = await self._get(url)
            with self._parsing(url):
                if urlsplit(final).path != "/works" or not soup.select_one("#main .work.index"):
                    raise SourceError(
                        "parse_error", "来源页面未识别为近期作品列表；不会静默更换入口或放宽条件。"
                    )
                items = [self._summary(row, final) for row in soup.select("li.work.blurb")]
                if language:
                    local_filter = SearchFilters(language=language)
                    items = [work for work in items if matches_filters(work, local_filter)]
                params = [
                    ("work_search[query]", ""),
                    ("work_search[sort_column]", "revised_at"),
                    ("work_search[sort_direction]", "desc"),
                    ("page", "1"),
                ]
                if language:
                    params.append(("work_search[language_id]", language))
                transition = self.base_url + "/works/search?" + urlencode(params)
                return Page(items, final, next_url=transition)
        return await self.search(
            "",
            warnings=warnings,
            categories=categories,
            rating=rating,
            language=language,
            page_url=page_url,
        )

    async def search(
        self,
        query: str = "",
        warnings=(),
        categories=(),
        page_url: str | None = None,
        *,
        rating: str = "",
        language: str = "",
        filters: SearchFilters | None = None,
    ) -> Page:
        if filters is not None:
            query, warnings, categories, rating, language = (
                filters.query,
                filters.warnings,
                filters.categories,
                filters.rating,
                filters.language,
            )
        if page_url:
            url = self.normalize_url(page_url, strip_consent=True)
            if urlsplit(url).path != "/works/search":
                raise SourceError("invalid_url", "分页链接不是作品搜索入口。")
        else:
            params = [
                ("work_search[query]", query),
                ("work_search[sort_column]", "revised_at"),
                ("work_search[sort_direction]", "desc"),
                ("page", "1"),
            ]
            params += [
                ("work_search[archive_warning_ids][]", v)
                for v in self._ids(warnings, WARNING_OPTIONS, WARNING_IDS)
            ]
            params += [
                ("work_search[category_ids][]", v)
                for v in self._ids(categories, CATEGORY_OPTIONS, CATEGORY_IDS)
            ]
            if rating:
                params.append(
                    ("work_search[rating_ids]", self._ids([rating], RATING_OPTIONS, RATING_IDS)[0])
                )
            if language:
                params.append(("work_search[language_id]", language))
            url = self.base_url + "/works/search?" + urlencode(params)
        soup, final = await self._get(url)
        with self._parsing(url):
            rows = soup.select("li.work.blurb")
            # AO3 omits the list entirely for zero results: the results heading is the proof.
            heading = _text(soup.select_one("#main h2.heading"))
            if (
                not rows
                and not soup.select_one("#main ol.work.index")
                and not re.search(r"\b0\s+Works?\b", heading, re.I)
            ):
                raise SourceError("parse_error", "来源页面未识别为搜索结果；不会当作搜索耗尽。")
            items = [self._summary(row, final) for row in rows]
            return Page(
                items,
                final,
                self._pagination(soup, final, "next"),
                self._pagination(soup, final, "previous"),
            )

    def _pagination(self, soup, url: str, direction: str) -> str | None:
        node = soup.select_one(f".pagination li.{direction} a")
        return (
            self.normalize_url(node.get("href", ""), from_url=url, strip_consent=True)
            if node
            else None
        )

    def _summary(self, row: Tag, url: str) -> WorkSummary:
        heading = row.select_one('h4.heading a[href*="/works/"]')
        work_url = self._link(heading.get("href"), url) if heading else None
        if not heading or not work_url:
            raise SourceError("parse_error", "作品列表结构发生变化，无法识别作品链接。")
        ident = re.search(r"/works/(\d+)", work_url)
        if not ident:
            raise SourceError("parse_error", "作品链接缺少作品编号。")
        symbols = [e.get("title", "") for e in row.select(".required-tags [title]")]
        rating = next((v for v in symbols if v in RATING_IDS), "")
        cats = next(
            (v.split(", ") for v in symbols if all(t in CATEGORY_IDS for t in v.split(", "))), []
        )
        warnings = [_text(e) for e in row.select("li.warnings a")]
        if not warnings:
            warnings = [w for w in WARNING_IDS if any(w in v for v in symbols)]
        count, total = self._chapter_count(row)
        return WorkSummary(
            ident[1],
            work_url,
            _text(heading),
            [_text(a) for a in row.select('a[rel="author"]')],
            _text(row.select_one(".summary")),
            warnings,
            cats,
            rating,
            _text(row.select_one("dd.language")),
            count,
            total,
            _text(row.select_one(".datetime")),
        )

    @staticmethod
    def _chapter_count(node) -> tuple[int | None, int | None]:
        value = _text(node.select_one("dd.chapters"))
        left, _, right = value.partition("/")
        return _integer(left), _integer(right)

    def _work_meta(self, soup, url: str) -> WorkSummary:
        title = soup.select_one("#workskin h2.title")
        match = re.search(r"/works/(\d+)", urlsplit(url).path)
        if not title or not match:
            raise SourceError("parse_error", "没有识别到作品标题与编号，当前内容已保留。")
        root = soup.select_one("#workskin")
        metadata = soup.select_one("dl.work.meta") or root
        count, total = self._chapter_count(metadata)

        def meta(selector):
            return [_text(e) for e in metadata.select(selector)]

        return WorkSummary(
            match[1],
            self.base_url + "/works/" + match[1],
            _text(title),
            [_text(e) for e in root.select('.byline a[rel="author"]')],
            _text(root.select_one(".preface > .summary .userstuff")),
            meta("dd.warning.tags a"),
            meta("dd.category.tags a"),
            _text(metadata.select_one("dd.rating.tags a")),
            _text(metadata.select_one("dd.language")),
            count,
            total,
            _text(metadata.select_one("dd.status")),
        )

    def _series_refs(self, soup, url: str) -> list[SeriesRef]:
        result = []
        for a in soup.select('dd.series a[href*="/series/"]'):
            href = self._link(a.get("href"), url)
            match = re.search(r"/series/(\d+)", href or "")
            if match and not any(s.id == match[1] for s in result):
                parent = a.find_parent(class_="position") or a.parent
                position = re.search(r"Part\s+(\d+)", _text(parent), re.I)
                result.append(
                    SeriesRef(match[1], href, _text(a), int(position[1]) if position else None)
                )
        return result

    def _directory(self, soup, work: WorkSummary) -> list[ChapterRef]:
        chapters = []
        for index, option in enumerate(soup.select("select#selected_id option"), start=1):
            ident = option.get("value", "")
            if str(ident).isdigit():
                chapters.append(
                    ChapterRef(
                        str(ident), work.url + "/chapters/" + str(ident), _text(option), index
                    )
                )
        return chapters

    def _parse_chapter(self, soup, url: str) -> Chapter:
        work = self._work_meta(soup, url)
        container = soup.select_one('#chapters .chapter > .userstuff[role="article"]')
        if container is None:
            container = soup.select_one("#chapters > .userstuff")
        if container is None:
            raise SourceError(
                "parse_error", "作品页已打开，但正文结构未被识别；不会显示摘要代替正文。"
            )
        # Work on a copy so cached documents and later metadata extraction stay intact.
        body = BeautifulSoup(str(container), "html.parser")
        for unwanted in body.select("script, style, .landmark, noscript"):
            unwanted.decompose()
        for br in body.find_all("br"):
            br.replace_with("\n")
        # Boundaries rather than p-only selection preserve text in divs, mixed bare
        # text, headings and quotations without duplicating nested blocks.
        for block in body.select("p, li, div, blockquote, h1, h2, h3, h4, h5, h6, pre, tr"):
            block.insert_before("\n\n")
            block.insert_after("\n\n")
        for cell in body.select("td, th"):
            cell.insert_after("\t")
        text = _clean(body.get_text()).strip()
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        if not paragraphs:
            raise SourceError("parse_error", "正文容器为空，无法开始阅读。")
        match = re.search(r"/chapters/(\d+)", urlsplit(url).path)
        ident = match[1] if match else "work-" + work.id
        previous = next_url = None
        for a in soup.select("li.chapter a"):
            href = self._link(a.get("href"), url)
            if not href or "/chapters/" not in href:
                continue
            classes = a.parent.get("class", [])
            label = _text(a).casefold()
            if "previous" in classes or "previous chapter" in label:
                previous = href
            if "next" in classes or "next chapter" in label:
                next_url = href
        title = _text(soup.select_one("#chapters .chapter .preface h3.title")) or work.title
        directory = self._directory(soup, work)
        position = next((c.position for c in directory if c.id == ident), 1)
        return Chapter(
            ident,
            url,
            title,
            paragraphs,
            work,
            previous,
            next_url,
            self._series_refs(soup, url),
            position,
        )

    async def get_work(self, url_or_id: str) -> WorkDetail:
        raw = str(url_or_id)
        url = self.base_url + "/works/" + raw if raw.isdigit() else raw
        url = self.normalize_url(url)
        if not re.fullmatch(r"/works/\d+(?:/chapters/\d+)?", urlsplit(url).path):
            raise SourceError("invalid_url", "请提供作品链接或编号。")
        soup, final = await self._get(url)
        with self._parsing(url):
            chapter = self._parse_chapter(soup, final)
            return WorkDetail(
                chapter.work, chapter, self._directory(soup, chapter.work), chapter.series
            )

    async def get_chapter(self, url: str) -> Chapter:
        return (await self.get_work(url)).chapter

    async def get_series(self, url: str) -> SeriesDetail:
        url = self.normalize_url(url)
        match = re.fullmatch(r"/series/(\d+)", urlsplit(url).path)
        if not match:
            raise SourceError("invalid_url", "请提供系列链接。")
        soup, final = await self._get(url)
        with self._parsing(url):
            title = soup.select_one("#main h2.heading")
            if not title or not soup.select_one("#main .series.work.index"):
                raise SourceError("parse_error", "来源页面未识别为系列，无法推断下一篇。")
            return SeriesDetail(
                match[1],
                final,
                _text(title),
                [self._summary(row, final) for row in soup.select("li.work.blurb")],
                self._pagination(soup, final, "next"),
                self._pagination(soup, final, "previous"),
            )
