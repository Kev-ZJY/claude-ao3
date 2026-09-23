"""Controlled transport failures; no AO3 requests or remote prose."""

import asyncio
from pathlib import Path

import httpx
import pytest

from mini_notes.source import AO3Source, SourceError

BASE = "https://reader.example"
HTML = (Path(__file__).parent / "fixtures/ao3_listing.html").read_text()


@pytest.mark.parametrize(
    "exception,kind",
    [
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.ConnectError, "connect_error"),
        (httpx.ProxyError, "proxy_error"),
        (httpx.RemoteProtocolError, "remote_protocol_error"),
    ],
)
def test_transport_failure_is_classified_without_leaking_error_text(exception, kind):
    seen = []

    def handler(request):
        seen.append(request)
        raise exception("secret-password proxy.example/private", request=request)

    async def run():
        async with AO3Source(BASE, transport=httpx.MockTransport(handler), max_retries=0) as source:
            with pytest.raises(SourceError) as caught:
                await source.search()
            error = caught.value
            assert error.code == "network" and error.failure_kind == kind
            assert error.retryable and error.attempts == 1 and error.http_status is None
            assert source.health["last_failure_kind"] == kind
            assert "secret" not in str(error) and "proxy.example" not in repr(source.health)
            assert source.health["state"] == "idle"

    asyncio.run(run())
    assert len(seen) == 1


@pytest.mark.parametrize(
    "status,header,expected", [(429, None, 60), (429, "20", 20), (503, "30", 30)]
)
def test_retry_after_is_effective_and_not_retried_early(status, header, expected):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"Retry-After": header} if header else {})

    async def run():
        async with AO3Source(
            BASE, transport=httpx.MockTransport(handler), max_retries=1, min_interval=0
        ) as source:
            with pytest.raises(SourceError) as caught:
                await source.search()
            assert caught.value.retry_after == expected
            assert caught.value.http_status == status
            assert caught.value.failure_kind == f"http_{status}"
            assert caught.value.attempts == 1
            with pytest.raises(SourceError) as cooling:
                await source.search("different")
            assert expected - 1 < cooling.value.retry_after <= expected
            assert cooling.value.attempts == 0
            assert source.health["state"] == "cooldown"

    asyncio.run(run())
    assert len(seen) == 1


def test_http_525_is_distinct_and_never_claims_which_server_generated_it():
    async def run():
        async with AO3Source(
            BASE, transport=httpx.MockTransport(lambda r: httpx.Response(525)), max_retries=0
        ) as source:
            with pytest.raises(SourceError) as caught:
                await source.search()
            assert caught.value.failure_kind == "http_525"
            assert caught.value.http_status == 525 and caught.value.retryable
            assert "源站证书" not in caught.value.message

    asyncio.run(run())


def test_retry_closes_response_and_releases_gate_before_background_backoff():
    seen = []
    closed = []

    class EmptyStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""

        async def aclose(self):
            closed.append(True)

    async def run():
        sleeping, resume = asyncio.Event(), asyncio.Event()

        def handler(request):
            query = request.url.params.get("work_search[query]")
            seen.append(query)
            if query == "background" and seen.count(query) == 1:
                return httpx.Response(525, stream=EmptyStream())
            return httpx.Response(200, text=HTML)

        async with AO3Source(
            BASE, transport=httpx.MockTransport(handler), min_interval=0, max_retries=1
        ) as source:

            async def pause(attempt):
                assert closed == [True]
                sleeping.set()
                await resume.wait()

            source._retry_pause = pause

            async def background():
                async with source.request_context(priority="background"):
                    return await source.search("background")

            loading = asyncio.create_task(background())
            await asyncio.wait_for(sleeping.wait(), 1)
            assert (await asyncio.wait_for(source.search("foreground"), 1)).items
            resume.set()
            assert (await loading).items

    asyncio.run(run())
    assert seen == ["background", "foreground", "background"]


def test_cancellation_during_retry_wait_releases_all_source_state():
    async def run():
        sleeping = asyncio.Event()

        async with AO3Source(
            BASE,
            transport=httpx.MockTransport(lambda r: httpx.Response(525)),
            max_retries=1,
            min_interval=0,
        ) as source:

            async def pause(attempt):
                sleeping.set()
                await asyncio.Event().wait()

            source._retry_pause = pause
            task = asyncio.create_task(source.search())
            await asyncio.wait_for(sleeping.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert source.health["state"] == "idle"
            assert source.health["queued_requests"] == 0

    asyncio.run(run())


def test_network_policy_is_explicit_and_default_does_not_disable_environment(monkeypatch):
    seen = []

    class FakeClient:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run():
        for mode in ("system", "direct"):
            async with AO3Source(network=mode, http_client="httpx", max_retries=9) as source:
                assert source.network == mode and source.max_retries == 1
        with pytest.raises(SourceError) as caught:
            AO3Source(network="automatic")
        assert caught.value.code == "invalid_network"

    asyncio.run(run())
    assert [kwargs["trust_env"] for kwargs in seen] == [True, False]
    assert all(kwargs.get("verify", True) is True for kwargs in seen)


def test_legacy_error_construction_has_compatible_retryability():
    assert SourceError("network", "failure").retryable
    assert SourceError("unavailable", "failure").retryable
    assert not SourceError("forbidden", "stop").retryable
    assert not SourceError("network", "closed", retryable=False).retryable


def test_error_that_opens_circuit_already_advertises_required_wait():
    async def run():
        async with AO3Source(
            BASE,
            transport=httpx.MockTransport(lambda r: httpx.Response(525)),
            min_interval=0,
            max_retries=0,
        ) as source:
            with pytest.raises(SourceError) as first:
                await source.search("first")
            assert first.value.retry_after is None
            with pytest.raises(SourceError) as second:
                await source.search("second")
            assert second.value.retry_after == 10 and second.value.failure_kind == "http_525"
            with pytest.raises(SourceError) as third:
                await source.search("third")
            assert third.value.failure_kind == "circuit_open" and third.value.attempts == 0
            assert source.health["requests"] == 2

    asyncio.run(run())


def test_reading_state_and_cancellation_do_not_leave_gate_or_activity_busy():
    async def run():
        reading = asyncio.Event()

        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                reading.set()
                await asyncio.Event().wait()
                yield b""

        def handler(request):
            if request.url.params.get("work_search[query]") == "slow":
                return httpx.Response(200, stream=SlowBody())
            return httpx.Response(200, text=HTML)

        async with AO3Source(
            BASE, transport=httpx.MockTransport(handler), min_interval=0, max_retries=0
        ) as source:
            task = asyncio.create_task(source.search("slow"))
            await asyncio.wait_for(reading.wait(), 1)
            assert source.health["state"] == "reading"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert source.health["state"] == "idle"
            assert (await asyncio.wait_for(source.search("next"), 1)).items

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["nan", "inf", "-inf", "not a date"])
def test_nonfinite_retry_after_is_not_a_permanent_or_undefined_timer(invalid):
    assert AO3Source._retry_after(invalid) is None
