from django.utils import timezone

import pytest
from notifications.choices import ChannelType, NotifyOn
from notifications.models import Notification, NotificationDelivery, UserChannelBinding
from pydantic import SecretStr

from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def run_schedule(member_user, email_binding):
    """A daily schedule owned by ``member_user`` with notifications always on.

    Shared by the run-signal notification suites (``test_signals.py`` and
    ``test_run_signals.py``).
    """
    return ScheduledJob.objects.create(
        user=member_user,
        name="run-schedule",
        prompt="p",
        repos=[{"repo_id": "x/y", "ref": ""}],
        frequency=Frequency.DAILY,
        time="12:00",
        notify_on=NotifyOn.ALWAYS,
    )


@pytest.fixture
def rocketchat_configured():
    """Point ``site_settings`` at a fake Rocket Chat install with real ``SecretStr`` semantics.

    ``site_settings`` resolves fields via ``__getattr__`` rather than holding real instance
    attributes, so we poke ``__dict__`` directly on setup and pop on teardown. Using
    ``monkeypatch.setattr`` here would leak instance attributes past teardown and shadow
    the ``__getattr__`` fallback that later DB-backed fixtures rely on.
    """
    from core.site_settings import site_settings

    overrides = {
        "rocketchat_enabled": True,
        "rocketchat_url": "https://rc.example.com",
        "rocketchat_user_id": "botid",
        "rocketchat_auth_token": SecretStr("bottoken"),
    }
    for name, value in overrides.items():
        site_settings.__dict__[name] = value
    try:
        yield
    finally:
        for name in overrides:
            site_settings.__dict__.pop(name, None)


@pytest.fixture
def rocketchat_channel_enabled(db):
    # DB rows roll back with ``django_db``, but the ``SiteConfiguration`` cache is
    # process-local, so we evict it on teardown to avoid leaking ``enabled=True``
    # into later tests that expect the default (disabled) state.
    from core.models import SiteConfiguration

    config = SiteConfiguration.objects.get_instance()
    config.rocketchat_enabled = True
    config.save()
    try:
        yield config
    finally:
        SiteConfiguration._invalidate_cache()


@pytest.fixture
def email_binding(member_user):
    """Ensure the member_user has a verified email channel binding."""
    binding, _ = UserChannelBinding.objects.get_or_create(
        user=member_user,
        channel_type=ChannelType.EMAIL,
        defaults={"address": member_user.email, "is_verified": True, "verified_at": timezone.now()},
    )
    return binding


@pytest.fixture
def notification(member_user):
    return Notification.objects.create(
        recipient=member_user, event_type="schedule.finished", subject="Hi", body="Body", link_url="/x/"
    )


@pytest.fixture
def notification_with_delivery(member_user, email_binding):
    """Create a notification with a PENDING email delivery."""
    n = Notification.objects.create(
        recipient=member_user, event_type="schedule.finished", subject="s", body="b", link_url="/"
    )
    d = NotificationDelivery.objects.create(notification=n, channel_type="email", address=member_user.email)
    return n, d
