import importlib
from unittest.mock import Mock

from django.db import connection

import pytest

from core.encryption import decrypt_value


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
def test_seed_openai_row_enables_responses_api():
    """Real OpenAI fully implements ``/v1/responses``; the locked seed row should
    keep the pre-flag behavior. Other seeded rows opt out by default."""
    from core.models import Provider

    assert Provider.objects.get(slug="openai").use_responses_api is True
    for slug in ("anthropic", "google_genai", "openrouter"):
        assert Provider.objects.get(slug=slug).use_responses_api is False


@pytest.mark.django_db
def test_legacy_columns_dropped():
    """SiteConfiguration must no longer carry the encrypted provider key columns or openrouter_api_base."""
    with connection.cursor() as c:
        description = connection.introspection.get_table_description(c, "core_siteconfiguration")
    cols = {field.name for field in description}
    assert "_anthropic_api_key_encrypted" not in cols
    assert "_openai_api_key_encrypted" not in cols
    assert "_google_api_key_encrypted" not in cols
    assert "_openrouter_api_key_encrypted" not in cols
    assert "openrouter_api_base" not in cols


migration = importlib.import_module("core.migrations.0008_provider")

PROVIDER_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    for name in PROVIDER_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(migration, "_SECRETS_DIR", str(tmp_path))
    return tmp_path


def _run_seed() -> dict[str, dict]:
    """Run the seed against a fresh install (no legacy ``SiteConfiguration`` row) and return
    each provider's ``update_or_create`` defaults by slug."""
    provider = Mock()
    site_configuration = Mock()
    site_configuration.objects.filter.return_value.first.return_value = None
    models = {"Provider": provider, "SiteConfiguration": site_configuration}
    apps = Mock(get_model=Mock(side_effect=lambda app_label, name: models[name]))

    migration.seed_providers(apps, None)

    return {c.kwargs["slug"]: c.kwargs["defaults"] for c in provider.objects.update_or_create.call_args_list}


def test_seed_reads_provider_key_from_docker_secret(secrets_dir):
    (secrets_dir / "openrouter_api_key").write_text("sk-or-from-secret\n")

    rows = _run_seed()

    assert rows["openrouter"]["is_enabled"] is True
    assert decrypt_value(rows["openrouter"]["_api_key_encrypted"]) == "sk-or-from-secret"
    assert rows["anthropic"]["is_enabled"] is False
    assert rows["anthropic"]["_api_key_encrypted"] is None


def test_seed_prefers_docker_secret_over_env_var(secrets_dir, monkeypatch):
    (secrets_dir / "anthropic_api_key").write_text("sk-ant-from-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")

    rows = _run_seed()

    assert decrypt_value(rows["anthropic"]["_api_key_encrypted"]) == "sk-ant-from-secret"


def test_seed_falls_back_to_env_var_without_a_secret(secrets_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")

    rows = _run_seed()

    assert rows["openai"]["is_enabled"] is True
    assert decrypt_value(rows["openai"]["_api_key_encrypted"]) == "sk-from-env"


def test_seed_without_secret_or_env_var_leaves_providers_disabled(secrets_dir):
    rows = _run_seed()

    assert set(rows) == {"anthropic", "openai", "google_genai", "openrouter"}
    assert all(row["is_enabled"] is False and row["_api_key_encrypted"] is None for row in rows.values())
