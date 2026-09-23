"""Synthetic transport regressions; these do not claim AO3 is reachable."""

import json
import importlib.util
from pathlib import Path

import httpx
import pytest

from mini_notes.cli import diagnose, main
from mini_notes.curl_transport import CurlTransport
from mini_notes.source import AO3Source, SourceError


def test_system_mode_does_not_claim_an_unobserved_proxy_was_used(monkeypatch, capsys):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("mini_notes.network_diagnostics.sys.platform", "linux")
    main(["--network-info", "--http-client", "httpx", "--network", "system"])
    report = json.loads(capsys.readouterr().out)
    assert report["environment_proxy_used"] is None
    assert report["proxy_configured"] is False
    assert report["route_verified"] is False


def test_network_info_never_echoes_credentials_in_source(capsys):
    main(["--network-info", "--source", "https://private-user:private-secret@reader.example"])
    assert "private-" not in capsys.readouterr().out


async def test_doctor_records_failed_request_and_never_calls_it_direct_acceptance(monkeypatch):
    def factory(origin, **kwargs):
        async def fail(request):
            raise httpx.ConnectTimeout("sensitive transport message")
        return AO3Source(origin, transport=httpx.MockTransport(fail), min_interval=0, **kwargs)

    monkeypatch.setattr("mini_notes.cli.AO3Source", factory)
    report, code = await diagnose("https://reader.example", 1, "direct")
    assert code == 2 and report["ok"] is False
    assert len(report["requests"]) == 1  # A measurement must not hide a retry.
    assert report["requests"][0]["status"] is None
    assert report["requests"][0]["outcome"] == "connect_timeout"
    assert report["network_configuration"]["environment_proxy_used"] is False
    assert report["mainland_direct_verified"] is False
    assert report["started_at"] and report["finished_at"]
    assert report["code_sha256"]
    assert "sensitive" not in json.dumps(report)


async def test_doctor_rejects_secret_source_without_echoing_it():
    report, code = await diagnose("https://private-user:private-secret@reader.example", 1)
    assert code == 2
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize("exit_code,kind,retryable", [
    (5, "proxy_dns_error", False), (6, "dns_error", True),
    (35, "tls_handshake_error", True), (60, "tls_certificate_error", False),
    (77, "tls_ca_error", False), (97, "proxy_error", False),
])
async def test_curl_failure_survives_source_boundary(exit_code, kind, retryable):
    count = 0

    async def fail(request):
        nonlocal count
        count += 1
        CurlTransport._raise_exit(exit_code, request)

    async with AO3Source(transport=httpx.MockTransport(fail), min_interval=0,
                         retry_delay=0) as source:
        with pytest.raises(SourceError) as caught:
            await source.search(language="zh")
    assert caught.value.failure_kind == kind
    assert caught.value.curl_exit_code == exit_code
    assert caught.value.retryable is retryable
    assert count == (2 if retryable else 1)


async def test_live_acceptance_returns_failure_when_all_requests_are_denied(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/live_network_v41.py"
    spec = importlib.util.spec_from_file_location("live_network_acceptance", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def denied_source(**kwargs):
        return AO3Source(transport=httpx.MockTransport(lambda _: httpx.Response(403)),
                         min_interval=0, **kwargs)

    monkeypatch.setattr(module, "AO3Source", denied_source)
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "evidence.json")
    code = await module.main()
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert report["ok"] is False
    assert code == 2  # Shell success must not disagree with failed acceptance.
