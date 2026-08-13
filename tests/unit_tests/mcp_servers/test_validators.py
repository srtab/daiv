from __future__ import annotations

from django.core.exceptions import ValidationError

import pytest
from mcp_servers.validators import validate_http_url


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp_rt:8000/mcp",  # Docker Compose service name with an underscore
        "http://mcp-rt:8000/mcp",  # single-label host (no TLD)
        "http://sandbox:8000",  # single-label host, no path
        "http://localhost:8000/mcp",  # localhost
        "http://127.0.0.1:8000/mcp",  # bare IPv4
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
