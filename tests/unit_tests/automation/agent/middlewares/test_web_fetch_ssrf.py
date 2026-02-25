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
