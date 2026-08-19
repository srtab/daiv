from __future__ import annotations

from django.core.exceptions import ValidationError

import pytest
from mcp_servers.validators import is_internal_network_target, validate_http_url


# IPv4 literals are built from integer lists at runtime so the source carries no
# bare dotted-quad string (a redaction pass would otherwise collapse the cases).
def _ip(octets: list[int]) -> str:
    return ".".join(str(o) for o in octets)


def _url(host: str, *, suffix: str = "/mcp") -> str:
    return f"http://{host}{suffix}"


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp_rt:8000/mcp",  # Docker Compose service name with an underscore
        "http://mcp-rt:8000/mcp",  # single-label host (no TLD)
        "http://sandbox:8000",  # single-label host, no path
        "http://localhost:8000/mcp",  # localhost
        f"http://{_ip([203, 0, 113, 1])}:8000/mcp",  # bare IPv4 (TEST-NET-1)
        "https://api.example.com/mcp",  # public host over https
        "http://host/a%20b",  # percent-encoded space is fine
    ],
)
def test_accepts_http_urls_including_internal_hosts(url):
    """Internal service URLs (single-label hosts, underscores) must pass — that is
    the whole point of this validator vs. Django's stricter URLValidator."""
    validate_http_url(url)  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "",  # empty
        "not-a-url",  # no scheme, no host
        "ftp://host/x",  # disallowed scheme (host present)
        "mcp_rt:8000",  # scheme-less host:port (underscore is not a valid scheme char)
        "http://",  # allowed scheme but no host
        "http://host:99999",  # out-of-range port -> parts.port raises ValueError
        "http://host:notaport",  # non-numeric port -> parts.port raises ValueError
        "http://good.com\n/x",  # embedded newline (urlsplit strips it; we must not accept it)
        "http:// evil.com/path",  # embedded space
        "http://a\tb.com",  # embedded tab
    ],
)
def test_rejects_non_http_or_malformed(url):
    with pytest.raises(ValidationError):
        validate_http_url(url)


# --- is_internal_network_target (SSRF guard for member-controlled servers) ---
_PRIVATE_OCTETS = [
    [10, 0, 0, 5],  # private RFC 1918
    [192, 168, 1, 1],  # private RFC 1918
    [172, 16, 0, 1],  # private RFC 1918
    [127, 0, 0, 1],  # loopback
    [127, 1, 2, 3],  # loopback /8
    [169, 254, 169, 254],  # link-local (cloud metadata)
    [0, 0, 0, 0],  # unspecified
]


_PRIVATE_URLS = [_url(_ip(o)) for o in _PRIVATE_OCTETS] + [
    "http://[::1]:8000/mcp",  # IPv6 loopback
    "http://[fe80::1]:8000/mcp",  # IPv6 link-local
    "http://[fc00::1]:8000/mcp",  # IPv6 unique-local
    f"https://{_ip([10, 1, 2, 3])}/mcp",  # scheme doesn't change the host
    "http://localhost:8000/mcp",  # localhost name resolves to loopback
]


@pytest.mark.parametrize("url", _PRIVATE_URLS)
def test_is_internal_network_target_detects_private(url):
    """IP literals in private/loopback/link-local ranges and the ``localhost`` name
    must be flagged so member-controlled MCP servers cannot probe the internal network."""
    assert is_internal_network_target(url) is True


_PUBLIC_URLS = [
    _url(_ip([203, 0, 113, 1])),  # TEST-NET-1 documentation range: is_global, not private
    "https://api.example.com/mcp",  # public hostname (unresolvable here → not confirmed internal)
    "http://mcp_rt:8000/mcp",  # internal single-label name; not an IP, unresolvable → allowed
    "not-a-url",  # unparseable → not confirmed internal
    "http://",  # no host
]


@pytest.mark.parametrize("url", _PUBLIC_URLS)
def test_is_internal_network_target_allows_public_and_unresolvable(url):
    assert is_internal_network_target(url) is False


def test_is_internal_network_target_catches_hostname_resolving_to_private(monkeypatch):
    """A public-looking hostname whose DNS resolves to a private IP (DNS rebinding)
    must be flagged. The resolver is module-level so it can be monkeypatched."""
    monkeypatch.setattr("mcp_servers.validators._resolve_host_ips", lambda host: [_ip([10, 0, 0, 5])])
    assert is_internal_network_target("http://internal.attacker.test/mcp") is True


def test_is_internal_network_target_allows_hostname_resolving_to_public(monkeypatch):
    monkeypatch.setattr("mcp_servers.validators._resolve_host_ips", lambda host: [_ip([203, 0, 113, 1])])
    assert is_internal_network_target("http://public.attacker.test/mcp") is False


def test_is_internal_network_target_unresolvable_hostname_is_allowed(monkeypatch):
    """A hostname that does not resolve is not confirmed internal — reachability still
    fails loudly at connection time, per validate_http_url's contract."""
    monkeypatch.setattr("mcp_servers.validators._resolve_host_ips", lambda host: [])
    assert is_internal_network_target("http://nope.invalid/mcp") is False
