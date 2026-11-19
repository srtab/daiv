from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

import pytest

from core.models import Provider, ProviderFailureKind

SETTINGS_URL = reverse("site_configuration", kwargs={"group_key": "providers"})


def _mark(slug, kind, when=None, model="gpt-x"):
    when = when or timezone.now()
    Provider.objects.filter(slug=slug).update(
        failure_kind=kind, failure_model=model, last_failed_at=when, failing_since=when, is_enabled=True
    )
    Provider.invalidate_cache()


@pytest.mark.django_db
def test_an_admin_sees_the_banner_with_a_link_to_provider_settings(admin_client):
    _mark("openrouter", ProviderFailureKind.OUT_OF_CREDITS, model="glm-5.2")

    html = admin_client.get(reverse("dashboard")).content.decode()

    assert 'role="alert"' in html
    assert "OpenRouter" in html
    assert "out of credits" in html
    assert "glm-5.2" in html
    assert SETTINGS_URL in html
    assert "Open provider settings" in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        (ProviderFailureKind.SPEND_LIMIT_REACHED, "has hit its spending limit"),
        (ProviderFailureKind.AUTH_FAILED, "has been rejecting DAIV's API key"),
    ],
)
def test_the_banner_describes_each_admin_kind(admin_client, kind, fragment):
    _mark("anthropic", kind)

    assert fragment in admin_client.get(reverse("dashboard")).content.decode()


@pytest.mark.django_db
def test_a_member_never_sees_the_banner(member_client):
    _mark("openrouter", ProviderFailureKind.OUT_OF_CREDITS)

    html = member_client.get(reverse("dashboard")).content.decode()

    assert "out of credits" not in html
    assert SETTINGS_URL not in html


@pytest.mark.django_db
def test_the_banner_is_gone_after_the_ttl(admin_client):
    _mark("openrouter", ProviderFailureKind.OUT_OF_CREDITS, when=timezone.now() - timedelta(hours=25))

    assert "out of credits" not in admin_client.get(reverse("dashboard")).content.decode()


@pytest.mark.django_db
def test_no_banner_without_a_marked_provider(admin_client):
    assert "Open provider settings" not in admin_client.get(reverse("dashboard")).content.decode()
