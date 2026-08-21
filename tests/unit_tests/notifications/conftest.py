from django.utils import timezone

import pytest
from notifications.choices import ChannelType
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
def telegram_configured():
    """Point ``site_settings`` at a fake Telegram bot with real ``SecretStr`` semantics.

    Same discipline as ``rocketchat_configured``: ``site_settings`` resolves fields via
    ``__getattr__`` rather than holding real instance attributes, so we poke ``__dict__``
    directly and pop on teardown. ``monkeypatch.setattr`` would leak an instance attribute
    past teardown that shadows the ``__getattr__`` fallback later DB-backed fixtures need.
    """
    from core.site_settings import site_settings

    overrides = {
        "telegram_enabled": True,
        "telegram_bot_username": "daiv_bot",
        "telegram_bot_token": SecretStr("123:ABC"),
        "telegram_webhook_secret": SecretStr("s3cret"),
    }
    for name, value in overrides.items():
        site_settings.__dict__[name] = value
    try:
        yield
    finally:
        for name in overrides:
            site_settings.__dict__.pop(name, None)


@pytest.fixture
def site_settings_override():
    """Temporarily override ``site_settings`` fields, popping them on teardown.

    Use this instead of ``monkeypatch.setattr(site_settings, …)``. ``site_settings`` resolves
    fields through ``__getattr__``, so monkeypatch reads a *computed* value as the "old" one and
    restores it as a real instance attribute — which then shadows ``__getattr__`` for every
    later test, silently ignoring any DB-backed fixture that sets the same field.

    Call it once with several keywords, or repeatedly to change a value mid-test.
    """
    from core.site_settings import site_settings

    applied: set[str] = set()

    def _override(**values) -> None:
        for name, value in values.items():
            site_settings.__dict__[name] = value
            applied.add(name)

    try:
        yield _override
    finally:
        for name in applied:
            site_settings.__dict__.pop(name, None)


@pytest.fixture
def telegram_channel_enabled(db):
    # Mirrors ``rocketchat_channel_enabled``: DB rows roll back with ``django_db``, but the
    # ``SiteConfiguration`` cache is process-local, so evict it on teardown.
    from core.models import SiteConfiguration

    config = SiteConfiguration.objects.get_instance()
    config.telegram_enabled = True
    config.telegram_bot_username = "daiv_bot"
    config.telegram_bot_token = "123:ABC"  # noqa: S105 — test constant
    config.telegram_webhook_secret = "s3cret"  # noqa: S105 — test constant
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
