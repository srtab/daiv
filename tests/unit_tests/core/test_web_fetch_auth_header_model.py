from __future__ import annotations

from django.core.cache import cache
from django.db import IntegrityError

import pytest
from pydantic import SecretStr

from core.models import WEB_FETCH_AUTH_HEADERS_CACHE_KEY, WebFetchAuthHeader


@pytest.mark.django_db
class TestWebFetchAuthHeaderModel:
    def test_header_value_round_trips_through_encryption(self, make_auth_header):
        row = make_auth_header("context7.com", "X-API-Key", "sk-abc")
        row.refresh_from_db()
        assert row.header_value == "sk-abc"
        assert row._header_value_encrypted != "sk-abc"
        assert row._header_value_encrypted is not None

    def test_setting_blank_value_clears_encrypted_column(self, make_auth_header):
        row = make_auth_header("context7.com", "X-API-Key", "sk-abc")
        row.header_value = ""
        row.save()
        row.refresh_from_db()
        assert row._header_value_encrypted is None
        assert row.header_value is None

    def test_unique_domain_header_pair(self, make_auth_header):
        make_auth_header("context7.com", "X-API-Key", "a")
        with pytest.raises(IntegrityError):
            make_auth_header("context7.com", "X-API-Key", "b")

    def test_same_header_name_allowed_on_different_domains(self, make_auth_header):
        make_auth_header("context7.com", "X-API-Key", "a")
        make_auth_header("api.example.com", "X-API-Key", "b")
        assert WebFetchAuthHeader.objects.count() == 2

    def test_default_ordering(self, make_auth_header):
        make_auth_header("b.com", "A", "v")
        make_auth_header("a.com", "B", "v")
        make_auth_header("a.com", "A", "v")
        rows = list(WebFetchAuthHeader.objects.values_list("domain", "header_name"))
        assert rows == [("a.com", "A"), ("a.com", "B"), ("b.com", "A")]


@pytest.mark.django_db
class TestWebFetchAuthHeaderCache:
    def setup_method(self):
        cache.delete(WEB_FETCH_AUTH_HEADERS_CACHE_KEY)

    def test_get_cached_returns_dict_grouped_by_domain(self, make_auth_header):
        make_auth_header("context7.com", "X-API-Key", "sk-abc")
        make_auth_header("context7.com", "X-Trace", "trace-1")
        make_auth_header("api.example.com", "Authorization", "Bearer xyz")

        result = WebFetchAuthHeader.get_cached()

        assert set(result.keys()) == {"context7.com", "api.example.com"}
        assert isinstance(result["context7.com"]["X-API-Key"], SecretStr)
        assert result["context7.com"]["X-API-Key"].get_secret_value() == "sk-abc"
        assert result["context7.com"]["X-Trace"].get_secret_value() == "trace-1"
        assert result["api.example.com"]["Authorization"].get_secret_value() == "Bearer xyz"

    def test_get_cached_skips_rows_with_null_value(self, make_auth_header):
        make_auth_header("ok.com", "X-Key", "ok")
        broken = make_auth_header("bad.com", "X-Key", "x")
        broken._header_value_encrypted = None
        broken.save()

        result = WebFetchAuthHeader.get_cached()
        assert "bad.com" not in result
        assert "ok.com" in result

    def test_save_invalidates_cache(self, make_auth_header):
        make_auth_header("a.com", "X", "1")
        WebFetchAuthHeader.get_cached()  # populate
        assert cache.get(WEB_FETCH_AUTH_HEADERS_CACHE_KEY) is not None

        make_auth_header("b.com", "Y", "2")
        assert cache.get(WEB_FETCH_AUTH_HEADERS_CACHE_KEY) is None

    def test_delete_invalidates_cache(self, make_auth_header):
        row = make_auth_header("a.com", "X", "1")
        WebFetchAuthHeader.get_cached()
        assert cache.get(WEB_FETCH_AUTH_HEADERS_CACHE_KEY) is not None

        row.delete()
        assert cache.get(WEB_FETCH_AUTH_HEADERS_CACHE_KEY) is None
