from __future__ import annotations

from django.urls import reverse

import pytest

from core.forms import WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX as PREFIX
from core.models import WebFetchAuthHeader


def _management(total: int, initial: int = 0) -> dict[str, str]:
    return {
        f"{PREFIX}-TOTAL_FORMS": str(total),
        f"{PREFIX}-INITIAL_FORMS": str(initial),
        f"{PREFIX}-MIN_NUM_FORMS": "0",
        f"{PREFIX}-MAX_NUM_FORMS": "1000",
    }


@pytest.mark.django_db
class TestSiteConfigurationViewWithAuthHeaders:
    def test_get_renders_formset(self, admin_client, make_auth_header):
        make_auth_header("context7.com", "X-API-Key", "sk-abc")
        response = admin_client.get(reverse("site_configuration", kwargs={"group_key": "web_fetch"}))
        assert response.status_code == 200
        body = response.content.decode()
        assert "context7.com" in body
        assert "X-API-Key" in body
        # Raw value not in body
        assert "sk-abc" not in body
        assert f'name="{PREFIX}-TOTAL_FORMS"' in body

    def test_post_creates_row(self, admin_client):
        data = {
            **_management(total=1),
            f"{PREFIX}-0-domain": "context7.com",
            f"{PREFIX}-0-header_name": "X-API-Key",
            f"{PREFIX}-0-header_value": "sk-abc",
        }
        response = admin_client.post(reverse("site_configuration", kwargs={"group_key": "web_fetch"}), data=data)
        assert response.status_code == 302
        assert WebFetchAuthHeader.objects.filter(domain="context7.com", header_name="X-API-Key").exists()

    def test_post_deletes_marked_row(self, admin_client, make_auth_header):
        row = make_auth_header("context7.com", "X-API-Key", "sk-abc")
        data = {
            **_management(total=1, initial=1),
            f"{PREFIX}-0-id": str(row.pk),
            f"{PREFIX}-0-domain": "context7.com",
            f"{PREFIX}-0-header_name": "X-API-Key",
            f"{PREFIX}-0-header_value": "",
            f"{PREFIX}-0-DELETE": "on",
        }
        admin_client.post(reverse("site_configuration", kwargs={"group_key": "web_fetch"}), data=data)
        assert not WebFetchAuthHeader.objects.filter(pk=row.pk).exists()

    def test_save_is_atomic_when_headers_formset_invalid(self, admin_client):
        """When the headers formset has an invalid row, nothing is saved."""
        # A row with a domain but no header_name is invalid (header_name is required).
        data = {
            **_management(total=1),
            f"{PREFIX}-0-domain": "context7.com",
            f"{PREFIX}-0-header_name": "",  # required field missing → formset invalid
            f"{PREFIX}-0-header_value": "sk-abc",
        }
        admin_client.post(reverse("site_configuration", kwargs={"group_key": "web_fetch"}), data=data)
        assert not WebFetchAuthHeader.objects.exists()

    def test_env_locked_ignores_submitted_formset(self, admin_client, make_auth_header, monkeypatch):
        """When ``DAIV_WEB_FETCH_AUTH_HEADERS`` is set, a POST that tries to
        create a row OR delete an existing one must be ignored — env-locked
        config must not be mutable through the UI.
        """
        from core import site_settings as ss_module

        existing = make_auth_header("kept.example.com", "X-Old", "old-value")
        monkeypatch.setenv("DAIV_WEB_FETCH_AUTH_HEADERS", '{"x.com": {"H": "v"}}')
        ss_module._docker_secret_cache.clear()

        data = {
            **_management(total=2, initial=1),
            # Try to delete the existing row.
            f"{PREFIX}-0-id": str(existing.pk),
            f"{PREFIX}-0-domain": "kept.example.com",
            f"{PREFIX}-0-header_name": "X-Old",
            f"{PREFIX}-0-header_value": "",
            f"{PREFIX}-0-DELETE": "on",
            # Try to create a new row.
            f"{PREFIX}-1-domain": "evil.example.com",
            f"{PREFIX}-1-header_name": "X-New",
            f"{PREFIX}-1-header_value": "smuggled",
        }
        admin_client.post(reverse("site_configuration", kwargs={"group_key": "web_fetch"}), data=data)

        assert WebFetchAuthHeader.objects.filter(pk=existing.pk).exists()
        assert not WebFetchAuthHeader.objects.filter(domain="evil.example.com").exists()
