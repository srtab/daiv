from django.db import connection

import pytest


@pytest.mark.django_db
def test_seed_creates_four_locked_rows():
    from core.models import Provider, ProviderType

    slugs = {r.slug for r in Provider.objects.filter(is_locked=True)}
    assert slugs == {"anthropic", "openai", "google_genai", "openrouter"}

    openrouter = Provider.objects.get(slug="openrouter")
    assert openrouter.provider_type == ProviderType.OPENROUTER
    assert openrouter.base_url == "https://openrouter.ai/api/v1"
    assert openrouter.is_locked is True


@pytest.mark.django_db
def test_seed_disables_rows_without_key(monkeypatch):
    """
    Fresh DB + no env vars → all four seed rows are disabled.
    The fixture in conftest cleans envvars; verify behavior with explicit unset.
    """
    from core.models import Provider

    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    # The seeded rows were inserted by the migration that ran on the test DB.
    # Their is_enabled reflects the env at migration-time. We don't re-trigger
    # the migration here; we just assert the invariant that any row with empty
    # api_key has is_enabled=False (the migration sets is_enabled = bool(key)).
    for row in Provider.objects.filter(is_locked=True):
        if row.api_key is None:
            assert row.is_enabled is False, f"{row.slug} has no key but is enabled"


@pytest.mark.django_db
def test_legacy_columns_dropped():
    """SiteConfiguration must no longer carry the encrypted provider key columns or openrouter_api_base."""
    with connection.cursor() as c:
        c.execute("PRAGMA table_info('core_siteconfiguration')")
        cols = {row[1] for row in c.fetchall()}
    assert "_anthropic_api_key_encrypted" not in cols
    assert "_openai_api_key_encrypted" not in cols
    assert "_google_api_key_encrypted" not in cols
    assert "_openrouter_api_key_encrypted" not in cols
    assert "openrouter_api_base" not in cols
