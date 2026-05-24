"""Tests for the model catalog JSON view."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.urls import reverse

import pytest

from automation.agent.model_catalog.service import CatalogEntry
from core.models import Provider, ProviderType


@pytest.fixture
def providers(db):
    Provider.objects.filter(slug__in=["openrouter_test", "anthropic_test", "disabled_one"]).delete()
    Provider.objects.create(
        slug="openrouter_test",
        display_name="OpenRouter",
        provider_type=ProviderType.OPENROUTER,
        api_key="sk-1",
        is_enabled=True,
        sort_order=100,
    )
    Provider.objects.create(
        slug="anthropic_test",
        display_name="Anthropic",
        provider_type=ProviderType.ANTHROPIC,
        api_key="sk-2",
        is_enabled=True,
        sort_order=101,
    )
    Provider.objects.create(
        slug="disabled_one",
        display_name="Disabled",
        provider_type=ProviderType.OPENAI,
        api_key="sk-3",
        is_enabled=False,
        sort_order=102,
    )
    Provider.invalidate_cache()


@pytest.mark.django_db
def test_redirects_unauthenticated(client):
    response = client.get(reverse("automation:agent_models"))
    assert response.status_code in (302, 403)


@pytest.mark.django_db
def test_returns_providers_and_catalog(client, providers, django_user_model):
    user = django_user_model.objects.create_user(username="t", email="t@example.com", password="x")  # noqa: S106
    client.force_login(user)

    async def _fake_fetch_catalog(rows):
        return {
            "openrouter_test": CatalogEntry(models=["anthropic/claude-haiku-4.5"], error=None),
            "anthropic_test": CatalogEntry(models=[], error="API key not configured"),
        }

    with patch("automation.views.fetch_catalog", side_effect=_fake_fetch_catalog):
        response = client.get(reverse("automation:agent_models"))

    assert response.status_code == 200
    payload = json.loads(response.content)
    slugs = [p["slug"] for p in payload["providers"]]
    # Disabled providers are filtered out.
    assert "disabled_one" not in slugs
    assert "openrouter_test" in slugs and "anthropic_test" in slugs
    assert payload["catalog"]["openrouter_test"] == {"models": ["anthropic/claude-haiku-4.5"], "error": None}
    assert payload["catalog"]["anthropic_test"] == {"models": [], "error": "API key not configured"}


@pytest.mark.django_db
def test_empty_when_all_providers_disabled(client, db, django_user_model):
    Provider.objects.all().update(is_enabled=False)
    Provider.invalidate_cache()
    user = django_user_model.objects.create_user(username="t2", email="t2@example.com", password="x")  # noqa: S106
    client.force_login(user)

    async def _fake_fetch_catalog(rows):
        return {}

    with patch("automation.views.fetch_catalog", side_effect=_fake_fetch_catalog):
        response = client.get(reverse("automation:agent_models"))

    payload = json.loads(response.content)
    assert payload["providers"] == []
    assert payload["catalog"] == {}
