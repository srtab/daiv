import pytest
from configuration.forms import (
    PROVIDERS_FORMSET_PREFIX,
    ProviderForm,
    SiteConfigurationForm,
    WebFetchAuthHeaderForm,
    build_provider_formset,
    build_web_fetch_auth_header_formset,
)

from core.models import Provider, ProviderType, SiteConfiguration, WebFetchAuthHeader


def _data(**overrides):
    base = {
        "slug": "myprov",
        "display_name": "My",
        "provider_type": ProviderType.OPENAI,
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-x",
        "extra_headers": "{}",
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
        instance=cfg, env_locked_fields=set(), field_defaults={}, data={"agent_model_name": "openai:gpt-5.4"}
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
        instance=cfg, env_locked_fields=set(), field_defaults={}, data={"agent_model_name": "openai:gpt-5.4"}
    )
    form.is_valid()
    assert any("disabled" in str(e) for e in form.errors.get("agent_model_name", []))


def _formset_data(rows: list[dict]) -> dict:
    """Build a POST dict for the providers formset from a list of row dicts."""
    fields: dict[str, str] = {
        f"{PROVIDERS_FORMSET_PREFIX}-TOTAL_FORMS": str(len(rows)),
        f"{PROVIDERS_FORMSET_PREFIX}-INITIAL_FORMS": "0",
        f"{PROVIDERS_FORMSET_PREFIX}-MIN_NUM_FORMS": "0",
        f"{PROVIDERS_FORMSET_PREFIX}-MAX_NUM_FORMS": "1000",
    }
    for idx, row in enumerate(rows):
        for key, value in row.items():
            fields[f"{PROVIDERS_FORMSET_PREFIX}-{idx}-{key}"] = str(value)
    return fields


@pytest.mark.django_db
def test_formset_rejects_duplicate_slug():
    row = {
        "slug": "vllm",
        "display_name": "vLLM",
        "provider_type": ProviderType.OPENAI,
        "base_url": "",
        "api_key": "sk-x",
        "extra_headers": "{}",
        "is_enabled": "on",
        "sort_order": "0",
    }
    data = _formset_data([row, row])
    formset = build_provider_formset()(data, queryset=Provider.objects.none(), prefix=PROVIDERS_FORMSET_PREFIX)
    assert not formset.is_valid()
    # ``slug`` is unique so Django's modelformset surfaces the duplicate before
    # the custom check runs; either message is acceptable.
    errors = str(formset.non_form_errors()) + str([f.errors for f in formset.forms])
    assert "duplicate" in errors.lower()


@pytest.mark.django_db
def test_formset_ignores_unsaved_row_removed_from_dom():
    """Phantom form for a DOM-removed empty row must not raise required-field errors."""
    empty_row = {
        "slug": "",
        "display_name": "",
        "provider_type": "",
        "base_url": "",
        "api_key": "",
        "extra_headers": "",
        "is_enabled": "",
        "sort_order": "0",
    }
    data = _formset_data([{}, empty_row])
    formset = build_provider_formset()(data, queryset=Provider.objects.none(), prefix=PROVIDERS_FORMSET_PREFIX)
    assert formset.is_valid(), [f.errors for f in formset.forms]


@pytest.mark.django_db
def test_formset_rejects_delete_on_locked_row():
    """A crafted POST that marks a locked seed row for delete must not 500 the request."""
    locked = Provider.objects.get(slug="anthropic")
    data = {
        f"{PROVIDERS_FORMSET_PREFIX}-TOTAL_FORMS": "1",
        f"{PROVIDERS_FORMSET_PREFIX}-INITIAL_FORMS": "1",
        f"{PROVIDERS_FORMSET_PREFIX}-MIN_NUM_FORMS": "0",
        f"{PROVIDERS_FORMSET_PREFIX}-MAX_NUM_FORMS": "1000",
        f"{PROVIDERS_FORMSET_PREFIX}-0-id": str(locked.pk),
        f"{PROVIDERS_FORMSET_PREFIX}-0-slug": locked.slug,
        f"{PROVIDERS_FORMSET_PREFIX}-0-display_name": locked.display_name,
        f"{PROVIDERS_FORMSET_PREFIX}-0-provider_type": locked.provider_type,
        f"{PROVIDERS_FORMSET_PREFIX}-0-base_url": locked.base_url,
        f"{PROVIDERS_FORMSET_PREFIX}-0-extra_headers": "{}",
        f"{PROVIDERS_FORMSET_PREFIX}-0-is_enabled": "on",
        f"{PROVIDERS_FORMSET_PREFIX}-0-sort_order": str(locked.sort_order),
        f"{PROVIDERS_FORMSET_PREFIX}-0-DELETE": "on",
    }
    formset = build_provider_formset()(
        data, queryset=Provider.objects.filter(pk=locked.pk), prefix=PROVIDERS_FORMSET_PREFIX
    )
    assert not formset.is_valid()
    assert any("Locked provider" in str(e) for e in formset.non_form_errors())


@pytest.mark.django_db
def test_form_rejects_slug_rename_on_saved_custom_row():
    """Slug is immutable after creation on any saved row, locked or not."""
    p = Provider.objects.create(slug="my-custom", display_name="My", provider_type=ProviderType.OPENAI, api_key="k")
    form = ProviderForm(data=_data(slug="renamed", provider_type=ProviderType.OPENAI, api_key=""), instance=p)
    assert not form.is_valid()
    assert "slug" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize(
    "base_url", ["https://llm.example.com", "https://api.example.com/", "https://api.example.com/openai"]
)
def test_form_warns_on_openai_base_url_missing_version_segment(base_url):
    form = ProviderForm(data=_data(base_url=base_url, provider_type=ProviderType.OPENAI))
    assert form.is_valid(), form.errors
    assert form.base_url_version_warning is not None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com/v1",
        "https://api.example.com/v1/",
        "https://api.example.com/v2beta/openai",
        "https://api.example.com/v1beta1",
        "https://api.example.com/v10",
        "https://api.example.com/V1/",  # case-insensitive — admins typing /V1 should not trip a false-positive
    ],
)
def test_form_no_warning_when_base_url_has_version_segment(base_url):
    form = ProviderForm(data=_data(base_url=base_url, provider_type=ProviderType.OPENAI))
    assert form.is_valid(), form.errors
    assert form.base_url_version_warning is None


@pytest.mark.django_db
def test_form_no_warning_for_non_openai_type():
    """Anthropic/Google/OpenRouter rows aren't subject to the OpenAI path-append quirk."""
    form = ProviderForm(data=_data(base_url="https://api.example.com", provider_type=ProviderType.ANTHROPIC))
    assert form.is_valid(), form.errors
    assert form.base_url_version_warning is None


@pytest.mark.django_db
def test_form_no_warning_when_base_url_empty():
    form = ProviderForm(data=_data(base_url="", provider_type=ProviderType.OPENAI, is_enabled=""))
    assert form.is_valid(), form.errors
    assert form.base_url_version_warning is None


@pytest.mark.django_db
def test_form_persists_use_responses_api_flag():
    form = ProviderForm(data=_data(use_responses_api="on"))
    assert form.is_valid(), form.errors
    saved = form.save()
    saved.refresh_from_db()
    assert saved.use_responses_api is True


@pytest.mark.django_db
def test_form_persists_verify_ssl_disabled():
    """Unchecking the box stores ``verify_ssl=False`` (admin opted out of TLS verification)."""
    form = ProviderForm(data=_data())  # _data omits verify_ssl → unchecked
    assert form.is_valid(), form.errors
    saved = form.save()
    saved.refresh_from_db()
    assert saved.verify_ssl is False


@pytest.mark.django_db
def test_form_persists_verify_ssl_enabled():
    form = ProviderForm(data=_data(verify_ssl="on"))
    assert form.is_valid(), form.errors
    saved = form.save()
    saved.refresh_from_db()
    assert saved.verify_ssl is True


@pytest.mark.django_db
def test_form_verify_ssl_initial_is_true_on_new_row():
    """The form's checkbox renders checked by default — admins must opt out, not in."""
    form = ProviderForm()
    assert form.fields["verify_ssl"].initial is True


@pytest.mark.django_db
@pytest.mark.parametrize("ptype", [ProviderType.GOOGLE_GENAI, ProviderType.ANTHROPIC])
def test_form_warns_when_verify_ssl_disabled_on_unsupported_type(ptype):
    """Toggling verify_ssl=False on Google/Anthropic is a silent no-op at the SDK layer;
    the form surfaces this so the admin doesn't think they've opted out when they haven't."""
    form = ProviderForm(data=_data(provider_type=ptype, base_url=""))
    assert form.is_valid(), form.errors
    assert form.verify_ssl_warning is not None


@pytest.mark.django_db
def test_form_no_verify_ssl_warning_on_supported_type():
    """OpenAI / OpenRouter rows do thread the http_client through, so no warning."""
    form = ProviderForm(data=_data(provider_type=ProviderType.OPENAI))
    assert form.is_valid(), form.errors
    assert form.verify_ssl_warning is None


@pytest.mark.django_db
def test_form_no_verify_ssl_warning_when_enabled():
    form = ProviderForm(data=_data(provider_type=ProviderType.GOOGLE_GENAI, base_url="", verify_ssl="on"))
    assert form.is_valid(), form.errors
    assert form.verify_ssl_warning is None


@pytest.mark.django_db
def test_unbound_form_warnings_are_none():
    """Regression: ``_collect_provider_warnings`` must never raise on an empty/unbound form."""
    form = ProviderForm()
    assert form.base_url_version_warning is None
    assert form.verify_ssl_warning is None


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


@pytest.mark.django_db
class TestTelegramForm:
    def test_webhook_secret_is_never_a_form_field(self):
        # It is DAIV-generated. Rendering it as a typed secret input would invite an admin
        # to overwrite the value the fail-closed webhook route compares against.
        assert "telegram_webhook_secret" not in SiteConfigurationForm.SECRET_FIELDS
        assert "telegram_bot_token" in SiteConfigurationForm.SECRET_FIELDS

    def test_bot_username_is_not_editable(self):
        assert "telegram_bot_username" not in SiteConfigurationForm.Meta.fields

    def test_group_form_offers_only_the_toggle_and_the_token(self):
        group = SiteConfiguration.get_group_by_key("telegram")
        form = SiteConfigurationForm(instance=SiteConfiguration.objects.get_instance(), group=group)
        assert set(form.fields) == {"telegram_enabled", "telegram_bot_token"}


def test_cross_project_group_fields_are_bound_to_the_form():
    group = next(g for g in SiteConfiguration.get_field_groups() if g.key == "cross_project")
    assert group.toggle_field == "cross_project_access_enabled"
    assert set(group.fields) == {"cross_project_access_enabled", "cross_project_webhook_runs_enabled"}
    assert set(group.fields) <= set(SiteConfigurationForm.Meta.fields)
