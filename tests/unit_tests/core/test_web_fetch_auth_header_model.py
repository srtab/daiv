from __future__ import annotations

from django.db import IntegrityError

import pytest

from core.models import WebFetchAuthHeader


def _create_row(domain: str, header_name: str, header_value: str) -> WebFetchAuthHeader:
    """``header_value`` is an :class:`EncryptedFieldDescriptor`, not a Django field,
    so it must be set after construction rather than passed to ``__init__``."""
    row = WebFetchAuthHeader(domain=domain, header_name=header_name)
    row.header_value = header_value
    row.save()
    return row


@pytest.mark.django_db
class TestWebFetchAuthHeaderModel:
    def test_header_value_round_trips_through_encryption(self):
        row = _create_row("context7.com", "X-API-Key", "sk-abc")
        row.refresh_from_db()
        assert row.header_value == "sk-abc"
        assert row._header_value_encrypted != "sk-abc"
        assert row._header_value_encrypted is not None

    def test_setting_blank_value_clears_encrypted_column(self):
        row = _create_row("context7.com", "X-API-Key", "sk-abc")
        row.header_value = ""
        row.save()
        row.refresh_from_db()
        assert row._header_value_encrypted is None
        assert row.header_value is None

    def test_unique_domain_header_pair(self):
        _create_row("context7.com", "X-API-Key", "a")
        with pytest.raises(IntegrityError):
            _create_row("context7.com", "X-API-Key", "b")

    def test_same_header_name_allowed_on_different_domains(self):
        _create_row("context7.com", "X-API-Key", "a")
        _create_row("api.example.com", "X-API-Key", "b")
        assert WebFetchAuthHeader.objects.count() == 2

    def test_default_ordering(self):
        _create_row("b.com", "A", "v")
        _create_row("a.com", "B", "v")
        _create_row("a.com", "A", "v")
        rows = list(WebFetchAuthHeader.objects.values_list("domain", "header_name"))
        assert rows == [("a.com", "A"), ("a.com", "B"), ("b.com", "A")]
