from django.core.cache import cache

import pytest
from jobs.throttle import check_jobs_throttle


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def settings_with_rate(monkeypatch):
    def _set(rate: str):
        monkeypatch.setattr("core.site_settings.site_settings.jobs_throttle_rate", rate, raising=False)

    return _set


@pytest.mark.django_db
def test_allows_under_limit(member_user, settings_with_rate):
    settings_with_rate("3/minute")
    assert check_jobs_throttle(member_user) is True
    assert check_jobs_throttle(member_user) is True
    assert check_jobs_throttle(member_user) is True


@pytest.mark.django_db
def test_blocks_at_limit(member_user, settings_with_rate):
    settings_with_rate("2/minute")
    check_jobs_throttle(member_user)
    check_jobs_throttle(member_user)
    assert check_jobs_throttle(member_user) is False


@pytest.mark.django_db
def test_empty_rate_is_permissive(member_user, settings_with_rate):
    settings_with_rate("")
    assert check_jobs_throttle(member_user) is True


@pytest.mark.django_db
def test_invalid_rate_is_permissive(member_user, settings_with_rate):
    settings_with_rate("not-a-rate")
    assert check_jobs_throttle(member_user) is True


@pytest.mark.django_db
def test_per_user_buckets(admin_user, member_user, settings_with_rate):
    settings_with_rate("1/minute")
    assert check_jobs_throttle(admin_user) is True
    assert check_jobs_throttle(member_user) is True
    assert check_jobs_throttle(admin_user) is False
