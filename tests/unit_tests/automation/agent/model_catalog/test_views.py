"""Tests for the model catalog JSON view."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.urls import reverse

import pytest

from automation.agent.model_catalog.service import CatalogEntry
from core.models import Provider, ProviderType


@pytest.fixture
def url():
    return reverse("api:agent_models")


@pytest.fixture
def providers(db):
    # Last entry has an empty display_name to exercise the slug-derived label fallback.
    specs = [
        ("openrouter_test", "OpenRouter", ProviderType.OPENROUTER, True),
        ("anthropic_test", "Anthropic", ProviderType.ANTHROPIC, True),
        ("disabled_one", "Disabled", ProviderType.OPENAI, False),
        ("no_display_name", "", ProviderType.OPENAI, True),
    ]
    Provider.objects.filter(slug__in=[slug for slug, *_ in specs]).delete()
    for sort_order, (slug, display_name, provider_type, is_enabled) in enumerate(specs, start=100):
        Provider.objects.create(
            slug=slug,
            display_name=display_name,
            provider_type=provider_type,
            api_key="sk-test",
            is_enabled=is_enabled,
            sort_order=sort_order,
        )
    Provider.invalidate_cache()


@pytest.mark.django_db
def test_rejects_unauthenticated(client, url):
    response = client.get(url)
    assert response.status_code == 401


@pytest.mark.django_db
def test_returns_providers_and_catalog(client, providers, url, django_user_model):
    user = django_user_model.objects.create_user(username="t", email="t@example.com", password="x")  # noqa: S106
    client.force_login(user)

    captured_rows: list = []

    async def _fake_fetch_catalog(rows):
        captured_rows.extend(rows)
        return {
            "openrouter_test": CatalogEntry(models=["anthropic/claude-haiku-4.5"], error=None),
            "anthropic_test": CatalogEntry(models=[], error="API key not configured"),
            "no_display_name": CatalogEntry(models=["gpt-5"], error=None),
        }

    with patch("automation.api.views.fetch_catalog", side_effect=_fake_fetch_catalog):
        response = client.get(url)

    assert response.status_code == 200
    payload = json.loads(response.content)

    # Disabled providers are filtered before reaching fetch_catalog.
    captured_slugs = {row.slug for row in captured_rows}
    assert "disabled_one" not in captured_slugs
    assert {"openrouter_test", "anthropic_test", "no_display_name"} <= captured_slugs

    providers_by_slug = {p["slug"]: p["label"] for p in payload["providers"]}
    assert "disabled_one" not in providers_by_slug
    assert providers_by_slug["openrouter_test"] == "OpenRouter"
    assert providers_by_slug["anthropic_test"] == "Anthropic"
    # Empty display_name → slug-derived "Title Case" label.
    assert providers_by_slug["no_display_name"] == "No Display Name"

    assert payload["catalog"]["openrouter_test"] == {"models": ["anthropic/claude-haiku-4.5"], "error": None}
    assert payload["catalog"]["anthropic_test"] == {"models": [], "error": "API key not configured"}


@pytest.mark.django_db
def test_empty_when_all_providers_disabled(client, providers, url, django_user_model):
    Provider.objects.all().update(is_enabled=False)
    Provider.invalidate_cache()
    user = django_user_model.objects.create_user(username="t2", email="t2@example.com", password="x")  # noqa: S106
    client.force_login(user)

    async def _fake_fetch_catalog(rows):
        return {}

    with patch("automation.api.views.fetch_catalog", side_effect=_fake_fetch_catalog):
        response = client.get(url)

    payload = json.loads(response.content)
    assert payload["providers"] == []
    assert payload["catalog"] == {}
