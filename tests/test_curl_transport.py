"""Real local subprocesses emulate curl; no network or third-party prose."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from mini_notes.curl_transport import CurlResponseTooLarge, CurlTransport


@pytest.fixture
def curl_stub(tmp_path, monkeypatch):
    binary = tmp_path / "curl-stub"
    record = tmp_path / "record.json"
    binary.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, stat, sys, time
args = sys.argv[1:]
if '--version' in args:
    print('curl 8.7.1 local-fixture')
    print('Features: SSL ' + ('HTTP2' if os.environ.get('STUB_HTTP2', '1') == '1' else ''))
    sys.exit(0)
header_file = pathlib.Path(args[args.index('--header') + 1][1:])
header_data = header_file.read_text()
record = {'args': args, 'pid': os.getpid(), 'headers_file': str(header_file),
          'headers_mode': stat.S_IMODE(header_file.stat().st_mode),
          'directory_mode': stat.S_IMODE(header_file.parent.stat().st_mode),
          'has_cookie': 'fixture-cookie' in header_data,
          'has_encoding_header': 'accept-encoding:' in header_data.lower(),
          'proxy_present': bool(os.environ.get('HTTPS_PROXY'))}
pathlib.Path(os.environ['STUB_RECORD']).write_text(json.dumps(record))
mode = os.environ.get('STUB_MODE', 'success')
if mode == 'sleep':
    time.sleep(60)
elif mode == 'overflow':
    sys.stdout.buffer.write(b'HTTP/1.1 200 OK\r\n\r\n' + b'x' * 8_000_000)
    sys.stdout.buffer.flush()
    time.sleep(60)
elif mode.startswith('exit:'):
    sys.stderr.write('https://secret-user:secret-password@proxy.invalid private-path')
    sys.exit(int(mode.split(':')[1]))
elif mode == 'bad':
    sys.stdout.buffer.write(b'not-an-http-response')
elif mode == 'redirect':
    sys.stdout.buffer.write(b'HTTP/1.1 302 Found\r\nLocation: https://outside.invalid/\r\n\r\n')
elif mode == 'limited':
    sys.stdout.buffer.write(b'HTTP/1.1 429 Too Many Requests\r\nRetry-After: 37\r\n\r\nlimited')
else:
    sys.stdout.buffer.write(b'HTTP/1.1 103 Early Hints\r\nLink: </style>\r\n\r\n'
                           b'HTTP/2 200 OK\r\nContent-Type: text/plain\r\n'
                           b'Content-Encoding: gzip\r\nContent-Length: 999\r\n'
                           b'Set-Cookie: a=1\r\nSet-Cookie: b=2\r\n\r\noriginal fixture')
''')
    binary.chmod(0o700)
    monkeypatch.setenv("STUB_RECORD", str(record))
    return binary, record


def test_response_decoding_flags_private_headers_and_cleanup(curl_stub):
    binary, record = curl_stub

    async def run():
        transport = CurlTransport(curl_path=str(binary))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.get("https://fixture.invalid/works", headers={"Cookie": "fixture-cookie"})
            assert response.text == "original fixture" and response.http_version == "HTTP/2"
            assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
            assert "content-encoding" not in response.headers
            assert "content-length" not in response.headers
        evidence = json.loads(record.read_text())
        args = evidence["args"]
        assert args[0] == "--disable" and "--compressed" in args and "--http1.1" in args
        assert "--http2" not in args
        assert args[args.index("--retry") + 1] == "0"
        assert "--location" not in args and "--insecure" not in args
        assert "fixture-cookie" not in str(args) and evidence["has_cookie"]
        assert not evidence["has_encoding_header"]
        assert evidence["headers_mode"] == 0o600 and evidence["directory_mode"] == 0o700
        assert not Path(evidence["headers_file"]).exists()

    asyncio.run(run())


@pytest.mark.parametrize("mode,proxy_present", [("system", True), ("direct", False)])
def test_explicit_proxy_policy_and_http1_fallback(curl_stub, monkeypatch, mode, proxy_present):
    binary, record = curl_stub
    monkeypatch.setenv("HTTPS_PROXY", "http://fixture-proxy.invalid:9000")
    monkeypatch.setenv("STUB_HTTP2", "0")

    async def run():
        async with CurlTransport(curl_path=str(binary), network=mode) as transport:
            await transport.handle_async_request(httpx.Request("GET", "https://fixture.invalid/"))
        evidence = json.loads(record.read_text())
        assert evidence["proxy_present"] == proxy_present
        assert "--http1.1" in evidence["args"] and "--http2" not in evidence["args"]
        assert ("--noproxy" in evidence["args"]) == (mode == "direct")

    asyncio.run(run())


@pytest.mark.parametrize("mode,status", [("limited", 429), ("redirect", 302)])
def test_http_status_is_returned_without_hidden_retry_or_redirect(curl_stub, monkeypatch, mode, status):
    binary, record = curl_stub
    monkeypatch.setenv("STUB_MODE", mode)

    async def run():
        async with CurlTransport(curl_path=str(binary)) as transport:
            response = await transport.handle_async_request(httpx.Request("GET", "https://fixture.invalid/"))
            assert response.status_code == status
            if status == 429:
                assert response.headers["Retry-After"] == "37"
        assert Path(record).exists()

    asyncio.run(run())


@pytest.mark.parametrize("exit_code,error_type", [(5, httpx.ProxyError), (6, httpx.ConnectError), (28, httpx.TimeoutException), (60, httpx.ConnectError), (63, CurlResponseTooLarge)])
def test_safe_exit_code_mapping_never_emits_stderr(curl_stub, monkeypatch, exit_code, error_type):
    binary, record = curl_stub
    monkeypatch.setenv("STUB_MODE", f"exit:{exit_code}")

    async def run():
        async with CurlTransport(curl_path=str(binary)) as transport:
            with pytest.raises(error_type) as caught:
                await transport.handle_async_request(httpx.Request("GET", "https://fixture.invalid/"))
            assert "secret" not in str(caught.value) and "proxy.invalid" not in str(caught.value)
        assert not Path(json.loads(record.read_text())["headers_file"]).exists()

    asyncio.run(run())


async def _wait_record(record):
    for _ in range(100):
        if record.exists():
            return json.loads(record.read_text())
        await asyncio.sleep(.01)
    raise AssertionError("Local child did not start")


def test_macos_system_proxy_used_when_terminal_has_no_https_environment(curl_stub, monkeypatch):
    from mini_notes import network_diagnostics as nd
    binary, _ = curl_stub
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(nd.sys, "platform", "darwin")
    monkeypatch.setattr(nd.urllib.request, "getproxies_macosx_sysconf",
                        lambda: {"https": "http://127.0.0.1:7788"}, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost")
    transport = CurlTransport(curl_path=str(binary))
    effective = transport._environment()
    assert effective["https_proxy"] == "http://127.0.0.1:7788"
    assert effective["NO_PROXY"] == "localhost"
    assert "https_proxy" not in os.environ  # Never mutate the shell or OS settings.
    direct = CurlTransport(curl_path=str(binary), network="direct")._environment()
    assert not any(key.lower() == "https_proxy" for key in direct)


def test_explicit_proxy_environment_wins_over_system(curl_stub, monkeypatch):
    from mini_notes import network_diagnostics as nd
    binary, _ = curl_stub
    monkeypatch.setenv("HTTPS_PROXY", "http://explicit.invalid:9000")
    monkeypatch.setattr(nd.sys, "platform", "darwin")
    def forbidden():
        raise AssertionError("Do not read/override system proxy with explicit environment")
    monkeypatch.setattr(nd.urllib.request, "getproxies_macosx_sysconf", forbidden, raising=False)
    assert CurlTransport(curl_path=str(binary))._environment()["HTTPS_PROXY"] == "http://explicit.invalid:9000"


@pytest.mark.parametrize("action", ["cancel", "close", "timeout", "overflow"])
def test_termination_reaps_child_and_removes_private_files(curl_stub, monkeypatch, action):
    binary, record = curl_stub
    monkeypatch.setenv("STUB_MODE", "overflow" if action == "overflow" else "sleep")

    async def run():
        transport = CurlTransport(curl_path=str(binary), max_time=.15 if action == "timeout" else 5, max_bytes=1024)
        task = asyncio.create_task(transport.handle_async_request(httpx.Request("GET", "https://fixture.invalid/")))
        evidence = await _wait_record(record)
        if action == "cancel":
            task.cancel()
            expected = asyncio.CancelledError
        elif action == "close":
            await transport.aclose()
            expected = httpx.TransportError
        else:
            expected = CurlResponseTooLarge if action == "overflow" else httpx.TimeoutException
        with pytest.raises(expected):
            await asyncio.wait_for(task, 1)
        assert not transport._running
        with pytest.raises(ProcessLookupError):
            os.kill(evidence["pid"], 0)
        assert not Path(evidence["headers_file"]).exists()
        await transport.aclose()

    asyncio.run(run())


def test_invalid_requests_missing_binary_and_bad_response(curl_stub, monkeypatch):
    binary, _ = curl_stub
    with pytest.raises(httpx.UnsupportedProtocol):
        CurlTransport(curl_path="/nonexistent/original-fixture-curl")

    async def run():
        async with CurlTransport(curl_path=str(binary)) as transport:
            for request in (httpx.Request("POST", "https://fixture.invalid/"), httpx.Request("GET", "https://fixture.invalid/", content=b"body")):
                with pytest.raises(httpx.UnsupportedProtocol):
                    await transport.handle_async_request(request)
            monkeypatch.setenv("STUB_MODE", "bad")
            with pytest.raises(httpx.RemoteProtocolError):
                await transport.handle_async_request(httpx.Request("GET", "https://fixture.invalid/"))

    asyncio.run(run())


def test_timeout_flags_are_bounded_by_request_budget(curl_stub):
    binary, record = curl_stub

    async def run():
        async with CurlTransport(curl_path=str(binary), max_time=45) as transport:
            request = httpx.Request("GET", "https://fixture.invalid/", extensions={
                "timeout": {"connect": 100, "read": 37},
            })
            await transport.handle_async_request(request)
        args = json.loads(record.read_text())["args"]
        assert float(args[args.index("--connect-timeout") + 1]) == 10
        assert float(args[args.index("--max-time") + 1]) == 37

    asyncio.run(run())


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_spawn_window_cancellation_or_close_cannot_orphan_child(curl_stub, monkeypatch, action):
    binary, _ = curl_stub

    async def run():
        entered, proceed = asyncio.Event(), asyncio.Event()
        original, children = asyncio.create_subprocess_exec, []

        async def delayed_spawn(*args, **kwargs):
            entered.set()
            await proceed.wait()
            process = await original(*args, **kwargs)
            children.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        transport = CurlTransport(curl_path=str(binary))
        task = asyncio.create_task(transport.handle_async_request(httpx.Request("GET", "https://fixture.invalid/")))
        await entered.wait()
        closing = None
        if action == "cancel":
            task.cancel()
            expected = asyncio.CancelledError
        else:
            closing = asyncio.create_task(transport.aclose())
            await asyncio.sleep(0)
            expected = httpx.UnsupportedProtocol
        proceed.set()
        with pytest.raises(expected):
            await task
        if closing:
            await closing
        assert children and all(process.returncode is not None for process in children)
        assert not transport._running and not transport._spawning
        await transport.aclose()

    asyncio.run(run())
