"""Read-only, credential-free proxy environment diagnostics."""

from __future__ import annotations

import ipaddress
import os
import shutil
import sys
import urllib.request
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx


def public_origin(value: str) -> str:
    """Never echo credentials, paths or query strings from CLI input."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            return "[invalid source]"
        host = parsed.hostname
        if not all(c.isalnum() or c in ".-:" for c in host):
            return "[invalid source]"
        host = f"[{host}]" if ":" in host else host
        return f"https://{host}" + (f":{parsed.port}" if parsed.port else "")
    except ValueError:
        return "[invalid source]"


def network_configuration(network: str, http_client: str = "auto") -> dict:
    """Configuration evidence only: NO_PROXY and system routes are not observed."""
    selected = http_client
    if selected == "auto":
        selected = "curl" if shutil.which("curl") else "httpx"
    effective = curl_proxy_environment() if selected.startswith("curl") else dict(os.environ)
    return {
        "network": network,
        "http_client": http_client,
        "selected_client": selected,
        "application_proxy_policy": "disabled" if network == "direct" else "inherit",
        # Retained for consumers of the old field. A configured proxy is not
        # proof it was used for this URL (NO_PROXY may bypass it).
        "environment_proxy_used": False if network == "direct" else None,
        "proxy_configured": network == "system" and any(effective.get(k) for k in (
            "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")),
        "proxy_environment": proxy_snapshot(),
        "curl_effective_proxy": proxy_snapshot(effective)
            if network == "system" and selected.startswith("curl") else {},
        "route_verified": False,
        "exit_country": "unknown; not inferred from domain or proxy address",
        "scope": "Configuration only; direct disables application proxies, not VPN/TUN/router forwarding.",
    }


def curl_proxy_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep explicit proxy variables; inherit static macOS HTTPS proxy if absent.

    Curl itself doesn't read the macOS system settings that a browser uses. Read
    those through Python's system resolver, without touching shell/OS settings.
    PAC evaluation and system proxy authentication are not implemented here.
    """
    env = dict(os.environ if environ is None else environ)
    if sys.platform != "darwin" or any(env.get(key) for key in (
        "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
    )):
        return env
    resolver = getattr(urllib.request, "getproxies_macosx_sysconf", None)
    if resolver is not None:
        try:
            https = resolver().get("https")
            if https:
                env["https_proxy"] = https
        except (OSError, ValueError):
            pass  # A missing OS setting is not an instruction to invent a proxy.
    return env


def classify_transport_error(error: httpx.TransportError) -> str:
    """Use exception type only; exception messages can contain proxy credentials."""
    for kind, cls in (
        ("connect_timeout", httpx.ConnectTimeout),
        ("read_timeout", httpx.ReadTimeout),
        ("write_timeout", httpx.WriteTimeout),
        ("pool_timeout", httpx.PoolTimeout),
        ("timeout", httpx.TimeoutException),
        ("proxy_error", httpx.ProxyError),
        ("connect_error", httpx.ConnectError),
        ("read_error", httpx.ReadError),
        ("write_error", httpx.WriteError),
        ("remote_protocol_error", httpx.RemoteProtocolError),
        ("local_protocol_error", httpx.LocalProtocolError),
        ("unsupported_protocol", httpx.UnsupportedProtocol),
    ):
        if isinstance(error, cls):
            return kind
    return "transport_error"


def proxy_snapshot(environ: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Report configuration, never credentials, ports, paths or exit geography."""
    environ = os.environ if environ is None else environ
    result = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        value = environ.get(key)
        row = {"set": bool(value)}
        if value:
            try:
                parsed = urlsplit(value if "://" in value else "//" + value)
                host = parsed.hostname
                if not host or not all(c.isalnum() or c in ".-:" for c in host):
                    raise ValueError("Invalid proxy host")
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    loopback = host.lower() == "localhost"
                # Only recognized scheme names are emitted; never echo malformed input.
                scheme = (
                    parsed.scheme
                    if parsed.scheme in {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}
                    else None
                )
                row.update(scheme=scheme, hostname=host, loopback=loopback)
            except ValueError:
                row["parseable"] = False
        result[key] = row
    for key in ("NO_PROXY", "no_proxy"):
        result[key] = {"set": bool(environ.get(key))}
    return result
