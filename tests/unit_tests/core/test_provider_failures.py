from datetime import timedelta

from django.utils import timezone

import pytest

from core.models import Provider, ProviderFailureKind
from core.provider_failures import ProviderFailure, active_provider_failures, failure_message


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        (ProviderFailureKind.OUT_OF_CREDITS, "is out of credits"),
        (ProviderFailureKind.SPEND_LIMIT_REACHED, "spending limit"),
        (ProviderFailureKind.AUTH_FAILED, "rejected DAIV's API key"),
        (ProviderFailureKind.RATE_LIMITED, "rate-limiting DAIV"),
        (ProviderFailureKind.PROVIDER_UNAVAILABLE, "having problems"),
    ],
)
def test_failure_message_names_the_provider(kind, fragment):
    message = failure_message(kind, "openrouter")

    assert "OpenRouter" in message
    assert fragment in message


@pytest.mark.django_db
def test_failure_message_falls_back_to_the_slug_for_a_deleted_provider():
    assert failure_message(ProviderFailureKind.AUTH_FAILED, "gone").startswith("gone ")


@pytest.mark.django_db
def test_provider_failure_message_delegates():
    failure = ProviderFailure(kind=ProviderFailureKind.RATE_LIMITED, provider_slug="anthropic")

    assert failure.message() == failure_message(ProviderFailureKind.RATE_LIMITED, "anthropic")


@pytest.mark.django_db
def test_active_provider_failures_lists_only_admin_kinds_within_the_ttl():
    now = timezone.now()
    Provider.objects.filter(slug="openrouter").update(
        failure_kind=ProviderFailureKind.OUT_OF_CREDITS, last_failed_at=now, failing_since=now, is_enabled=True
    )
    Provider.objects.filter(slug="anthropic").update(
        failure_kind=ProviderFailureKind.RATE_LIMITED, last_failed_at=now, failing_since=now
    )
    Provider.objects.filter(slug="openai").update(
        failure_kind=ProviderFailureKind.AUTH_FAILED,
        last_failed_at=now - timedelta(hours=25),
        failing_since=now - timedelta(hours=25),
    )
    Provider.invalidate_cache()

    assert [row.slug for row in active_provider_failures()] == ["openrouter"]


@pytest.mark.django_db
def test_active_provider_failures_skips_disabled_providers():
    now = timezone.now()
    Provider.objects.filter(slug="openrouter").update(
        failure_kind=ProviderFailureKind.AUTH_FAILED, last_failed_at=now, failing_since=now, is_enabled=False
    )
    Provider.objects.filter(slug="anthropic").update(
        failure_kind=ProviderFailureKind.AUTH_FAILED, last_failed_at=now, failing_since=now, is_enabled=True
    )
    Provider.invalidate_cache()

    assert [row.slug for row in active_provider_failures()] == ["anthropic"]
