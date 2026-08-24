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
def rocketchat_configured(site_settings_override):
    """Point ``site_settings`` at a fake Rocket Chat install with real ``SecretStr`` semantics."""
    site_settings_override(
        rocketchat_enabled=True,
        rocketchat_url="https://rc.example.com",
        rocketchat_user_id="botid",
        rocketchat_auth_token=SecretStr("bottoken"),
    )


@pytest.fixture
def telegram_configured(site_settings_override):
    """Point ``site_settings`` at a fake Telegram bot with real ``SecretStr`` semantics."""
    site_settings_override(
        telegram_enabled=True,
        telegram_bot_username="daiv_bot",
        telegram_bot_token=SecretStr("123:ABC"),
        telegram_webhook_secret=SecretStr("s3cret"),
    )


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
def enabled_with_env_token(monkeypatch):
    """An env-locked token, which is the shape that never reaches the form's ``clean``."""
    from core.models import SiteConfiguration
    from core.site_settings import _docker_secret_cache, site_settings

    keys = ("DAIV_TELEGRAM_BOT_TOKEN", "DAIV_TELEGRAM_ENABLED")
    monkeypatch.setenv("DAIV_TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("DAIV_TELEGRAM_ENABLED", "true")
    for key in keys:
        _docker_secret_cache.pop(key, None)
    SiteConfiguration._invalidate_cache()
    try:
        yield site_settings
    finally:
        for key in keys:
            _docker_secret_cache.pop(key, None)
        SiteConfiguration._invalidate_cache()


@pytest.fixture
def configured_site(db):
    """Write fields onto the ``SiteConfiguration`` singleton and evict the cache afterwards.

    DB rows roll back with ``django_db``, but that cache is process-local, so leaving it warm
    leaks ``enabled=True`` into later tests that expect the default state.
    """
    from core.models import SiteConfiguration

    def _configure(**fields):
        config = SiteConfiguration.objects.get_instance()
        for name, value in fields.items():
            setattr(config, name, value)
        config.save()
        return config

    try:
        yield _configure
    finally:
        SiteConfiguration._invalidate_cache()


@pytest.fixture
def rocketchat_channel_enabled(configured_site):
    return configured_site(rocketchat_enabled=True)


@pytest.fixture
def telegram_channel_enabled(configured_site):
    return configured_site(
        telegram_enabled=True,
        telegram_bot_username="daiv_bot",
        telegram_bot_token="123:ABC",  # noqa: S106 — test constant
        telegram_webhook_secret="s3cret",  # noqa: S106 — test constant
    )


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
