import pytest

from core.forms import ProviderForm, SiteConfigurationForm
from core.models import Provider, ProviderType, SiteConfiguration


def _data(**overrides):
    base = {
        "slug": "myprov",
        "display_name": "My",
        "provider_type": ProviderType.OPENAI,
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-x",
        "extra_headers": "{}",
        "model_suggestions": "",
        "is_enabled": "on",
        "sort_order": 0,
    }
    base.update(overrides)
    return base


@pytest.mark.django_db
def test_form_accepts_valid_data():
    form = ProviderForm(data=_data())
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_form_rejects_reserved_google_slug():
    form = ProviderForm(data=_data(slug="google"))
    assert not form.is_valid()
    assert "slug" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["UPPER", "with space", "1-leading-digit", "with/slash", "x" * 33])
def test_form_rejects_malformed_slug(bad):
    form = ProviderForm(data=_data(slug=bad))
    assert not form.is_valid()
    assert "slug" in form.errors


@pytest.mark.django_db
def test_form_rejects_non_http_base_url():
    form = ProviderForm(data=_data(base_url="ftp://example.com"))
    assert not form.is_valid()
    assert "base_url" in form.errors


@pytest.mark.django_db
def test_form_rejects_empty_api_key_on_enabled_new_row():
    form = ProviderForm(data=_data(api_key=""))
    assert not form.is_valid()
    assert "api_key" in form.errors


@pytest.mark.django_db
def test_form_allows_empty_api_key_on_disabled_new_row():
    form = ProviderForm(data=_data(api_key="", is_enabled=""))
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_form_rejects_non_dict_extra_headers():
    form = ProviderForm(data=_data(extra_headers="[1, 2]"))
    assert not form.is_valid()
    assert "extra_headers" in form.errors


@pytest.mark.django_db
def test_form_rejects_invalid_header_name():
    form = ProviderForm(data=_data(extra_headers='{"Bad Header": "v"}'))
    assert not form.is_valid()
    assert "extra_headers" in form.errors


@pytest.mark.django_db
def test_form_locked_row_blocks_slug_change():
    """Disabled slug/type fields ignore submitted values; locked rows save unchanged."""
    p = Provider.objects.get(slug="anthropic")
    p.api_key = "k"
    p.save()
    form = ProviderForm(data=_data(slug="renamed", provider_type=ProviderType.OPENAI, api_key=""), instance=p)
    assert form.is_valid(), form.errors
    saved = form.save()
    saved.refresh_from_db()
    assert saved.slug == "anthropic"
    assert saved.provider_type == ProviderType.ANTHROPIC


@pytest.mark.django_db
def test_form_locked_row_allows_toggle_enabled():
    p = Provider.objects.get(slug="openai")
    p.api_key = "k"
    p.save()
    form = ProviderForm(data=_data(slug="openai", provider_type=ProviderType.OPENAI, is_enabled=""), instance=p)
    assert form.is_valid(), form.errors
    saved = form.save()
    assert saved.is_enabled is False


@pytest.mark.django_db
def test_form_keeps_existing_key_when_blank_on_update():
    p = Provider.objects.create(slug="myprov", display_name="My", provider_type=ProviderType.OPENAI, api_key="orig")
    form = ProviderForm(data=_data(api_key=""), instance=p)
    assert form.is_valid(), form.errors
    saved = form.save()
    saved.refresh_from_db()
    assert saved.api_key == "orig"


@pytest.mark.django_db
def test_site_form_flags_model_with_keyless_provider():
    p = Provider.objects.get(slug="openai")
    p.api_key = None
    p.is_enabled = True
    p.save()

    cfg = SiteConfiguration.objects.get_instance()
    form = SiteConfigurationForm(
        instance=cfg,
        env_locked_fields=set(),
        field_defaults={},
        data={"agent_model_name_provider": "openai", "agent_model_name_model": "gpt-5.4"},
    )
    form.is_valid()
    assert any("API key" in str(e) for e in form.errors.get("agent_model_name", []))


@pytest.mark.django_db
def test_site_form_flags_model_with_disabled_provider():
    p = Provider.objects.get(slug="openai")
    p.api_key = "k"
    p.is_enabled = False
    p.save()

    cfg = SiteConfiguration.objects.get_instance()
    form = SiteConfigurationForm(
        instance=cfg,
        env_locked_fields=set(),
        field_defaults={},
        data={"agent_model_name_provider": "openai", "agent_model_name_model": "gpt-5.4"},
    )
    form.is_valid()
    assert any("disabled" in str(e) for e in form.errors.get("agent_model_name", []))
