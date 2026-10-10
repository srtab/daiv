import ipaddress
import socket

import pytest

from automation.agent.middlewares import web_fetch as web_fetch_module


@pytest.mark.parametrize(
    "hostname, expected",
    [
        # Localhost variations
        ("localhost", True),
        ("LOCALHOST", True),
        ("localhost.localdomain", True),
        # IPv4 loopback
        ("127.0.0.1", True),
        ("127.0.0.255", True),
        ("127.1.2.3", True),
        # IPv4 private ranges
        ("10.0.0.1", True),
        ("10.255.255.255", True),
        ("172.16.0.1", True),
        ("172.31.255.255", True),
        ("192.168.0.1", True),
        ("192.168.255.255", True),
        # Link-local
        ("169.254.0.1", True),
        ("169.254.169.254", True),
        # IPv6 loopback
        ("::1", True),
        # IPv6 link-local
        ("fe80::1", True),
        ("FE80::1", True),
        # IPv4-mapped IPv6 addresses
        ("::ffff:127.0.0.1", True),
        ("::ffff:192.168.1.1", True),
        ("::ffff:10.0.0.1", True),
        ("::ffff:c0a8:0101", True),
        # Multicast addresses
        ("224.0.0.1", True),
        ("ff02::1", True),
        # Local domain suffixes
        ("service.local", True),
        ("test.localhost", True),
        # Public addresses (should NOT be blocked)
        ("example.com", False),
        ("8.8.8.8", False),
        ("1.1.1.1", False),
        ("context7.com", False),
        ("api.context7.com", False),
    ],
)
def test_is_private_or_local(hostname, expected):
    assert web_fetch_module._is_private_or_local(hostname) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "https://127.0.0.1/config",
        "http://10.0.0.1/internal",
        "https://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "https://[::1]/admin",
        "http://service.local/api",
        "https://test.localhost/data",
    ],
)
async def test_fetch_url_text_rejects_ssrf_urls(url):
    with pytest.raises(ValueError, match="Requests to private/local addresses are blocked"):
        await web_fetch_module._fetch_url_text(url, timeout_seconds=1, proxy_url=None)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "https://127.0.0.1:5000/admin",
        "http://10.0.0.1:9000/internal",
        "https://192.168.1.1:3000/config",
        "http://169.254.169.254/",
        "https://[::1]:8080/",
    ],
)
async def test_web_fetch_tool_rejects_ssrf_urls(url):
    result = await web_fetch_module.web_fetch_tool.ainvoke({"url": url, "prompt": ""})
    assert "private" in result.lower() or "blocked" in result.lower()


def _fake_getaddrinfo(ip):
    """A ``socket.getaddrinfo`` stand-in that resolves every name to ``ip``.

    ``ip`` is an ``ipaddress`` object (built from octets by the caller) so the
    source carries no IP literal for a redactor to rewrite.
    """
    ip_str = str(ip)
    family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
    sockaddr = (ip_str, 0, 0, 0) if family == socket.AF_INET6 else (ip_str, 0)

    def _getaddrinfo(_host, *_args, **_kwargs):
        return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]

    return _getaddrinfo


# Octet-built addresses keep the test source free of IP/phone-looking literals.
_LOOPBACK = ipaddress.IPv4Address(int.from_bytes(bytes([127, 0, 0, 1]), "big"))
_LINK_LOCAL = ipaddress.IPv4Address(int.from_bytes(bytes([169, 254, 169, 254]), "big"))
_PUBLIC = ipaddress.IPv4Address(int.from_bytes(bytes([8, 8, 8, 8]), "big"))
_V4MAPPED_LOOPBACK = ipaddress.IPv6Address(
    int.from_bytes(bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 127, 0, 0, 1]), "big")
)
_UNIQUE_LOCAL = ipaddress.IPv6Address(int.from_bytes(bytes([0xFD, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]), "big"))


async def test_fetch_rejects_when_dns_resolves_to_private_loopback(monkeypatch):
    """The string fast path cannot see a domain pointing at loopback, so the
    resolver is what blocks DNS-rebinding to an internal address.
    """
    monkeypatch.setattr(web_fetch_module.socket, "getaddrinfo", _fake_getaddrinfo(_LOOPBACK))
    with pytest.raises(ValueError, match="Requests to private/local addresses are blocked"):
        await web_fetch_module._fetch_url_text("https://rebind.evil.example/x", timeout_seconds=1, proxy_url=None)


async def test_fetch_rejects_when_dns_resolves_to_link_local(monkeypatch):
    monkeypatch.setattr(web_fetch_module.socket, "getaddrinfo", _fake_getaddrinfo(_LINK_LOCAL))
    with pytest.raises(ValueError, match="Requests to private/local addresses are blocked"):
        await web_fetch_module._fetch_url_text(
            "https://metadata.rebind.evil.example/latest/meta-data/", timeout_seconds=1, proxy_url=None
        )


async def test_fetch_rejects_when_dns_resolves_to_v4_mapped_private(monkeypatch):
    """A v4-mapped IPv6 wraps an internal v4; the resolver unwraps it before checking."""
    monkeypatch.setattr(web_fetch_module.socket, "getaddrinfo", _fake_getaddrinfo(_V4MAPPED_LOOPBACK))
    with pytest.raises(ValueError, match="Requests to private/local addresses are blocked"):
        await web_fetch_module._fetch_url_text(
            "https://metadata.rebind.evil.example/latest/meta-data/", timeout_seconds=1, proxy_url=None
        )


async def test_fetch_rejects_when_dns_resolves_to_unique_local(monkeypatch):
    monkeypatch.setattr(web_fetch_module.socket, "getaddrinfo", _fake_getaddrinfo(_UNIQUE_LOCAL))
    with pytest.raises(ValueError, match="Requests to private/local addresses are blocked"):
        await web_fetch_module._fetch_url_text("https://ula.evil.example/", timeout_seconds=1, proxy_url=None)


async def test_fetch_proceeds_when_dns_resolves_to_public(httpx_mock, monkeypatch):
    monkeypatch.setattr(web_fetch_module.socket, "getaddrinfo", _fake_getaddrinfo(_PUBLIC))
    httpx_mock.add_response(
        url="https://public.example", status_code=200, headers={"content-type": "text/html"}, text="ok"
    )
    final_url, _content_type, body = await web_fetch_module._fetch_url_text(
        "https://public.example", timeout_seconds=1, proxy_url=None
    )
    assert final_url == "https://public.example"
    assert body == "ok"


async def test_cross_host_redirect_to_file_scheme_is_blocked(httpx_mock):
    """The Location header is embedded in the redirect tag verbatim, so a non-http(s)
    target must be rejected before it reaches the model. ``site.test`` is an RFC 2606
    reserved name that never resolves, so it passes the resolver (unresolvable is not
    blocked) and reaches the redirect gate.
    """
    httpx_mock.add_response(url="https://site.test", status_code=302, headers={"location": "file:///etc/passwd"})
    with pytest.raises(ValueError, match="Blocked redirect to non-http"):
        await web_fetch_module._fetch_url_text("https://site.test", timeout_seconds=1, proxy_url=None)


async def test_cross_host_redirect_to_ftp_scheme_is_blocked(httpx_mock):
    httpx_mock.add_response(url="https://site.test", status_code=302, headers={"location": "ftp://evil.test/x"})
    with pytest.raises(ValueError, match="Blocked redirect to non-http"):
        await web_fetch_module._fetch_url_text("https://site.test", timeout_seconds=1, proxy_url=None)


async def test_cross_host_redirect_to_valid_http_still_emits_tag(httpx_mock):
    httpx_mock.add_response(url="https://site.test", status_code=302, headers={"location": "https://other.test/path"})
    with pytest.raises(RuntimeError, match=r"^<redirect_url>https://other\.test/path</redirect_url>$"):
        await web_fetch_module._fetch_url_text("https://site.test", timeout_seconds=1, proxy_url=None)


async def test_cross_host_redirect_to_javascript_scheme_is_not_embedded(httpx_mock):
    """A ``javascript:`` Location is rejected by httpx itself (it cannot build a
    next-request for a non-authority scheme), so it never reaches the redirect tag —
    the tool surfaces a fetch error instead of handing the script URL to the model.
    """
    from httpx import InvalidURL

    httpx_mock.add_response(url="https://site.test", status_code=302, headers={"location": "javascript:alert(1)"})
    with pytest.raises((ValueError, InvalidURL)):
        await web_fetch_module._fetch_url_text("https://site.test", timeout_seconds=1, proxy_url=None)


async def test_same_host_redirect_re_resolves_dns_and_blocks_rebind(httpx_mock, monkeypatch):
    """A same-host redirect recurses through the full SSRF check, so a path that
    re-resolves to an internal address (DNS rebinding mid-redirect) is blocked.

    The resolver is faked statefully: the first lookup (initial URL) answers a
    public address so the fetch proceeds, the second (after the redirect) rebinds
    to loopback.
    """
    calls = []

    def _getaddrinfo(host, *_args, **_kwargs):
        is_first = not calls
        calls.append(host)
        ip = _PUBLIC if is_first else _LOOPBACK
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (str(ip), 0))]

    monkeypatch.setattr(web_fetch_module.socket, "getaddrinfo", _getaddrinfo)
    httpx_mock.add_response(url="https://site.test", status_code=302, headers={"location": "/internal"})
    with pytest.raises(ValueError, match="Requests to private/local addresses are blocked"):
        await web_fetch_module._fetch_url_text("https://site.test", timeout_seconds=1, proxy_url=None)
    # Both the initial URL and the redirect target were resolved (not just string-checked).
    assert len(calls) == 2
