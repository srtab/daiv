from django.core.exceptions import ValidationError

import pytest

from core.validators import validate_searxng_url


@pytest.mark.parametrize(
    "url",
    [
        "http://searxng:8080/search",
        "http://my_searxng:8080/prefix/search",
        "http://my_searxng.internal:8080/prefix/search",
        "http://localhost:8080/search",
        "https://search.example.com/prefix/search",
        "https://search.example.com:8443/prefix/search/",
        "http://127.0.0.1:8080/search",
        "http://[::1]:8080/search",
    ],
)
def test_searxng_endpoint_accepts_internal_hosts_and_preserves_complete_urls(url):
    validate_searxng_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "/search",
        "searxng:8080/search",
        "ftp://search.example.com/search",
        "https:///search",
        "https://-invalid/search",
        "https://invalid_host-/search",
        "http://[::1/search",
        "http://searxng:bad/search",
        "http://searxng:65536/search",
        "https://user:secret@search.example.com/search",
        "https://user@search.example.com/search",
        "https://search.example.com/search?q=test",
        "https://search.example.com/search?",
        "https://search.example.com/search#results",
        "https://search.example.com/search#",
        "https://search.example.com/my path/search",
        "https://search.example.com/\nsearch",
        "https://search.example.com/\x7fsearch",
        "https://search.example.com\\search",
    ],
)
def test_searxng_endpoint_rejects_unsafe_or_ambiguous_urls_without_disclosing_them(url):
    with pytest.raises(ValidationError) as caught:
        validate_searxng_url(url)
    assert caught.value.code == "invalid"
    assert "secret" not in str(caught.value)
    assert "search.example.com" not in str(caught.value)
