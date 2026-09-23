import httpx
import pytest

from mini_notes.curl_transport import CurlResponseTooLarge, CurlTransport
from mini_notes.source import AO3Source, SourceError


async def test_auto_selects_curl_without_httpx_proxy_mounts(monkeypatch):
    monkeypatch.setattr("mini_notes.source.shutil.which", lambda _: "/usr/bin/curl")
    monkeypatch.setenv("HTTPS_PROXY", "http://fake-proxy.invalid:7654")
    async with AO3Source() as source:
        assert isinstance(source._client._transport, CurlTransport)
        assert not source._client._mounts  # HTTPX must not bypass the selected transport.
        assert source.health["transport"] == "curl/http1.1"


async def test_no_curl_uses_httpx_and_explicit_choice_is_honored(monkeypatch):
    monkeypatch.setattr("mini_notes.source.shutil.which", lambda _: None)
    async with AO3Source(network="direct") as source:
        assert isinstance(source._client._transport, httpx.AsyncHTTPTransport)
        assert source.health["transport"] == "httpx/http1.1"
    async with AO3Source(http_client="httpx", network="direct") as source:
        assert source.health["transport"] == "httpx/http1.1"
    with pytest.raises(SourceError) as error:
        AO3Source(http_client="curl")
    assert error.value.code == "invalid_transport"


async def test_oversized_curl_response_stops_without_retry():
    calls = 0
    async def oversized(request):
        nonlocal calls
        calls += 1
        raise CurlResponseTooLarge("fixture")
    async with AO3Source(transport=httpx.MockTransport(oversized), min_interval=0) as source:
        with pytest.raises(SourceError) as error:
            await source.search(language="zh")
        assert not error.value.retryable
        assert error.value.failure_kind == "response_too_large"
        assert calls == 1


async def test_native_generic_timeout_is_visible_and_retryable():
    async def timeout(request):
        raise httpx.TimeoutException("fixture")
    async with AO3Source(transport=httpx.MockTransport(timeout), max_retries=0) as source:
        with pytest.raises(SourceError) as error:
            await source.search(language="zh")
        assert error.value.failure_kind == "timeout"
        assert error.value.retryable
        assert "超时" in error.value.message


async def test_redirect_does_not_spend_an_automatic_retry_attempt():
    async def handler(request):
        if request.url.path == "/works/101":
            return httpx.Response(302, headers={"location": "/works/101/chapters/1001"})
        raise httpx.TimeoutException("fixture")
    async with AO3Source(transport=httpx.MockTransport(handler), max_retries=0, min_interval=0) as source:
        with pytest.raises(SourceError) as error:
            await source.get_work("101")
        assert source.health["requests"] == 2
        assert error.value.attempts == 1  # Work -> chapter is one attempt with two GETs.
