from django.test import RequestFactory

from core.templatetags.querystring_tags import querystring_without


def _context(query: str) -> dict:
    return {"request": RequestFactory().get(f"/x/?{query}")}


def test_drops_excluded_keys_and_ends_with_an_ampersand():
    assert querystring_without(_context("q=foo&page=3&role=admin"), "page") == "q=foo&role=admin&"


def test_keeps_repeated_values():
    assert querystring_without(_context("repo=a&repo=b&page=2"), "page") == "repo=a&repo=b&"


def test_returns_empty_when_nothing_remains():
    assert querystring_without(_context("page=2&status=done"), "page", "status") == ""
