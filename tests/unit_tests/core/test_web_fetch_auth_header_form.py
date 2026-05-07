from __future__ import annotations

import pytest

from core.forms import WebFetchAuthHeaderForm, build_web_fetch_auth_header_formset
from core.models import WebFetchAuthHeader


@pytest.mark.django_db
class TestWebFetchAuthHeaderForm:
    def test_valid_row_saves_encrypted_value(self):
        form = WebFetchAuthHeaderForm(
            data={"domain": "context7.com", "header_name": "X-API-Key", "header_value": "sk-abc"}
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        instance.refresh_from_db()
        assert instance.header_value == "sk-abc"

    def test_rejects_domain_with_scheme(self):
        form = WebFetchAuthHeaderForm(
            data={"domain": "https://context7.com", "header_name": "X-API-Key", "header_value": "v"}
        )
        assert not form.is_valid()
        assert "domain" in form.errors

    def test_rejects_domain_with_path(self):
        form = WebFetchAuthHeaderForm(
            data={"domain": "context7.com/v1", "header_name": "X-API-Key", "header_value": "v"}
        )
        assert not form.is_valid()
        assert "domain" in form.errors

    def test_rejects_invalid_header_name(self):
        form = WebFetchAuthHeaderForm(data={"domain": "context7.com", "header_name": "X API Key", "header_value": "v"})
        assert not form.is_valid()
        assert "header_name" in form.errors

    def test_partial_row_required_together(self):
        form = WebFetchAuthHeaderForm(data={"domain": "context7.com", "header_name": "X-API-Key", "header_value": ""})
        assert not form.is_valid()
        assert "header_value" in form.errors

    def test_existing_row_keeps_value_when_header_value_blank(self, make_auth_header):
        existing = make_auth_header("context7.com", "X-API-Key", "keep-me")
        form = WebFetchAuthHeaderForm(
            data={"domain": "context7.com", "header_name": "X-API-Key", "header_value": ""}, instance=existing
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        instance.refresh_from_db()
        assert instance.header_value == "keep-me"


def _management(total: int, initial: int = 0) -> dict[str, str]:
    return {
        "form-TOTAL_FORMS": str(total),
        "form-INITIAL_FORMS": str(initial),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
    }


@pytest.mark.django_db
class TestWebFetchAuthHeaderFormset:
    def test_empty_formset_is_valid(self):
        formset = build_web_fetch_auth_header_formset()(
            data=_management(total=0), queryset=WebFetchAuthHeader.objects.none()
        )
        assert formset.is_valid(), formset.errors

    def test_duplicate_pair_within_formset_rejected(self):
        data = {
            **_management(total=2),
            "form-0-domain": "context7.com",
            "form-0-header_name": "X-API-Key",
            "form-0-header_value": "a",
            "form-1-domain": "context7.com",
            "form-1-header_name": "X-API-Key",
            "form-1-header_value": "b",
        }
        formset = build_web_fetch_auth_header_formset()(data=data, queryset=WebFetchAuthHeader.objects.none())
        assert not formset.is_valid()

    def test_blank_row_dropped(self):
        data = {**_management(total=1), "form-0-domain": "", "form-0-header_name": "", "form-0-header_value": ""}
        formset = build_web_fetch_auth_header_formset()(data=data, queryset=WebFetchAuthHeader.objects.none())
        assert formset.is_valid(), formset.errors
        formset.save()
        assert WebFetchAuthHeader.objects.count() == 0
