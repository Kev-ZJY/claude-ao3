"""Optional curl subprocess transport for anonymous reads; no shell or curlrc.

The complete decoded response (headers included) is bounded in memory. HTTP
statuses are returned unchanged. The owning source retains redirect, decoding,
rate-limit and retry policy. Curl's total timeout is deliberately stricter than
HTTPX's per-read timeout; an external operation deadline may cancel it sooner.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import tempfile
from pathlib import Path

import httpx

from .network_diagnostics import curl_proxy_environment


class CurlResponseTooLarge(httpx.ReadError):
    """Local response budget exceeded; retrying the same response will not help."""

    retryable = False
    failure_kind = "response_too_large"


class CurlTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        network: str = "system",
        curl_path: str | None = None,
        max_bytes: int = 4_000_000,
        max_time: float = 45.0,
        http_version: str = "1.1",
    ):
        if network not in {"system", "direct"}:
            raise ValueError("network must be system or direct")
        if http_version not in {"1.1", "2"}:
            raise ValueError("http_version must be 1.1 or 2")
        self.http_version = http_version
        if not 0 < max_bytes <= 4_000_000:
            raise ValueError("max_bytes must be between 1 and 4,000,000")
        if not math.isfinite(max_time) or not 0 < max_time <= 45:
            raise ValueError("max_time must be between 0 and 45 seconds")
        binary = shutil.which(curl_path or "curl")
        if not binary:
            raise httpx.UnsupportedProtocol("未找到可用的 curl 程序。")
        self.binary = binary
        self.network, self.max_bytes, self.max_time = network, max_bytes, max_time
        self.http2: bool | None = None
        self._probe_lock = asyncio.Lock()
        self._running: set[asyncio.subprocess.Process] = set()
        self._spawning: set[asyncio.Task] = set()
        self._runs_done: set[asyncio.Future] = set()
        self._closed = False

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        # A killed child can still have a full stdout pipe. Waiting for exit
        # without draining that pipe deadlocks asyncio's pipe disconnection.
        # Only the owning _run reads stdout; aclose merely signals the child.
        if process.stdout is not None:
            while await process.stdout.read(65536):
                pass
        await process.wait()

    def _environment(self) -> dict[str, str]:
        env = curl_proxy_environment() if self.network == "system" else os.environ.copy()
        if self.network == "direct":
            for key in tuple(env):
                if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
                    env.pop(key)
        return env

    async def _run(self, args: list[str], *, timeout: float, limit: int) -> tuple[int, bytes]:
        if self._closed:
            raise httpx.UnsupportedProtocol("curl 传输已关闭。")
        finished = asyncio.get_running_loop().create_future()
        self._runs_done.add(finished)
        # Shield process creation so cancellation in the spawn window cannot
        # lose the child handle before cleanup gets a chance to terminate it.
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            self.binary, "--disable", *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._environment(),
        ))
        self._spawning.add(spawning)
        process = None
        try:
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                process = await spawning
                raise
            self._running.add(process)
            if self._closed:
                raise httpx.UnsupportedProtocol("curl 传输已关闭。")
            data = bytearray()
            async with asyncio.timeout(timeout):
                while chunk := await process.stdout.read(min(65536, limit + 1 - len(data))):
                    if len(data) + len(chunk) > limit:
                        raise CurlResponseTooLarge("响应超过本地读取上限，已停止读取。")
                    data.extend(chunk)
                code = await process.wait()
            return code, bytes(data)
        except TimeoutError:
            raise httpx.TimeoutException("curl 请求达到总等待上限。") from None
        except OSError:
            raise httpx.ConnectError("无法启动 curl 读取程序。") from None
        finally:
            try:
                if process is not None:
                    await self._stop(process)
            finally:
                self._spawning.discard(spawning)
                if process is not None:
                    self._running.discard(process)
                self._runs_done.discard(finished)
                finished.set_result(None)

    async def _probe(self):
        async with self._probe_lock:
            if self.http2 is not None:
                return
            code, output = await self._run(["--version"], timeout=3, limit=65536)
            if code or not output.startswith(b"curl "):
                raise httpx.UnsupportedProtocol("curl 能力检测失败。")
            match = re.match(rb"curl (\d+)\.(\d+)\.(\d+)", output)
            if not match or tuple(map(int, match.groups())) < (7, 55, 0):
                raise httpx.UnsupportedProtocol("需要 curl 7.55 或更新版本。")
            features = next((line for line in output.splitlines() if line.startswith(b"Features:")), b"")
            self.http2 = b"HTTP2" in features.split()

    @staticmethod
    def _parse(data: bytes, request: httpx.Request) -> httpx.Response:
        offset = 0
        while True:
            end = data.find(b"\r\n\r\n", offset)
            if end < 0 or end + 4 > 65536:
                raise httpx.RemoteProtocolError("curl 未返回有效的响应头。", request=request)
            lines = data[offset:end].split(b"\r\n")
            match = re.fullmatch(rb"(HTTP/(?:1\.[01]|2|3)) ([0-9]{3})(?: (.*))?", lines[0])
            if not match:
                raise httpx.RemoteProtocolError("curl 返回了无法识别的状态行。", request=request)
            version, status, reason = match.groups()
            status = int(status)
            headers = []
            for line in lines[1:]:
                if b":" not in line or line.startswith((b" ", b"\t")):
                    raise httpx.RemoteProtocolError("curl 返回了无法识别的响应头。", request=request)
                name, value = line.split(b":", 1)
                # --compressed already decodes the entity. HTTPX must neither
                # decode it again nor trust the encoded entity's byte length.
                if name.strip().lower() not in {b"content-encoding", b"content-length"}:
                    headers.append((name.strip(), value.strip()))
            offset = end + 4
            if 100 <= status < 200 and status != 101:
                continue
            return httpx.Response(
                status, headers=headers, stream=httpx.ByteStream(data[offset:]),
                extensions={"http_version": version, "reason_phrase": reason or b""},
                request=request,
            )

    @staticmethod
    def _raise_exit(code: int, request: httpx.Request):
        if code == 63:
            raise CurlResponseTooLarge("响应超过本地读取上限，已停止读取。", request=request)
        error_type = {
            2: httpx.UnsupportedProtocol, 3: httpx.UnsupportedProtocol,
            5: httpx.ProxyError, 6: httpx.ConnectError, 7: httpx.ConnectError,
            18: httpx.ReadError, 23: httpx.ReadError, 28: httpx.TimeoutException,
            35: httpx.ConnectError, 51: httpx.ConnectError, 60: httpx.ConnectError,
            77: httpx.ConnectError, 92: httpx.RemoteProtocolError,
            97: httpx.ProxyError,
        }.get(code, httpx.TransportError)
        # Exit status is useful for diagnostics; curl stderr can contain proxy
        # credentials, paths or remote content and is never retained or emitted.
        error = error_type(f"curl 请求未完成（退出码 {code}）。", request=request)
        error.curl_exit_code = code
        kinds = {
            5: "proxy_dns_error", 6: "dns_error", 35: "tls_handshake_error",
            51: "tls_certificate_error", 60: "tls_certificate_error", 77: "tls_ca_error",
        }
        if code in kinds:
            error.failure_kind = kinds[code]
        if code in {2, 3, 5, 51, 60, 77, 97}:
            error.retryable = False
        raise error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.scheme not in {"http", "https"}:
            raise httpx.UnsupportedProtocol("curl 传输仅支持 HTTP(S) GET。", request=request)
        if request.url.username or request.url.password:
            raise httpx.UnsupportedProtocol("请求地址不能包含账号凭据。", request=request)
        try:
            if request.content:
                raise httpx.UnsupportedProtocol("curl GET 不支持请求正文。", request=request)
        except httpx.RequestNotRead:
            raise httpx.UnsupportedProtocol("curl GET 不支持流式请求正文。", request=request) from None
        await self._probe()
        timeout = request.extensions.get("timeout") or {}

        def bounded(value, maximum):
            if value is None:
                return maximum
            if not math.isfinite(value) or value <= 0:
                raise httpx.TimeoutException("请求等待预算必须为正且有限。", request=request)
            return min(value, maximum)

        total = bounded(timeout.get("read"), self.max_time)
        connect = bounded(timeout.get("connect"), min(total, 10))
        with tempfile.TemporaryDirectory(prefix="mini-notes-curl-") as directory:
            header_file = Path(directory) / "headers"
            # Headers may contain server-issued cookies; keep them out of argv
            # and clean the private file on success, timeout or cancellation.
            with open(header_file, "xb") as handle:
                os.chmod(header_file, 0o600)
                for name, value in request.headers.raw:
                    if b"\n" in name + value or b"\r" in name + value:
                        raise httpx.LocalProtocolError("请求头包含非法换行。", request=request)
                    if name.lower() == b"accept-encoding":
                        continue  # curl negotiates only encodings compiled into this binary.
                    handle.write(name + b": " + value + b"\r\n")
            args = [
                "--silent", "--show-error", "--request", "GET", "--include",
                "--compressed",
                "--suppress-connect-headers", "--retry", "0", "--max-redirs", "0",
                "--http2" if self.http_version == "2" and self.http2 else "--http1.1",
                "--connect-timeout", str(connect), "--max-time", str(total),
                "--max-filesize", str(self.max_bytes),
                "--header", "@" + str(header_file),
            ]
            if self.network == "direct":
                args += ["--proxy", "", "--noproxy", "*"]
            args += ["--url", str(request.url)]
            code, output = await self._run(args, timeout=total, limit=self.max_bytes)
            if code:
                self._raise_exit(code, request)
            return self._parse(output, request)

    async def aclose(self):
        self._closed = True
        for process in tuple(self._running):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        # Spawn-window children observe _closed immediately after creation.
        # Wait for each run's cleanup, not for the outer caller (which may do
        # unrelated work or retry). Never create a concurrent stdout reader.
        await asyncio.gather(*(asyncio.shield(done) for done in tuple(self._runs_done)))
