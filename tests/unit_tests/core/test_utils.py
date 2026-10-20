from django.contrib.sites.models import Site

import pytest
from redis.exceptions import LockError, LockNotOwnedError

from core.utils import DEFAULT_LOCK_TIMEOUT, build_absolute_url, build_uri, locked_task, prefixed_email_subject


@pytest.mark.django_db
class BuildAbsoluteUrlTest:
    def test_builds_url_with_site_domain(self):
        Site.objects.filter(pk=1).update(domain="app.example.com")
        assert build_absolute_url("/notifications/") == "https://app.example.com/notifications/"

    def test_builds_url_with_bare_path(self):
        Site.objects.filter(pk=1).update(domain="app.example.com")
        assert build_absolute_url("/") == "https://app.example.com/"

    def test_uses_current_site_domain(self):
        Site.objects.filter(pk=1).update(domain="custom.domain.org")
        assert build_absolute_url("/activity/42/") == "https://custom.domain.org/activity/42/"


@pytest.mark.django_db
class PrefixedEmailSubjectTest:
    def test_prepends_site_name_in_brackets(self):
        Site.objects.filter(pk=1).update(name="DAIV")
        assert prefixed_email_subject("Welcome") == "[DAIV] Welcome"

    def test_returns_subject_unchanged_when_site_name_empty(self):
        Site.objects.filter(pk=1).update(name="")
        assert prefixed_email_subject("Welcome") == "Welcome"

    def test_is_idempotent_on_already_prefixed_subject(self):
        Site.objects.filter(pk=1).update(name="DAIV")
        assert prefixed_email_subject("[DAIV] Welcome") == "[DAIV] Welcome"

    def test_returns_subject_unchanged_when_site_missing(self, mocker):
        mocker.patch("core.utils.Site.objects.get_current", side_effect=Site.DoesNotExist)
        assert prefixed_email_subject("Welcome") == "Welcome"


class BuildUriTest:
    def test_append_path_to_base_uri(self):
        assert build_uri("https://api.example.com", "v1/endpoint") == "https://api.example.com/v1/endpoint"

    def test_handle_double_slash(self):
        assert build_uri("https://api.example.com/", "/v1/endpoint") == "https://api.example.com/v1/endpoint"

    def test_handle_missing_slash(self):
        assert build_uri("https://api.example.com", "v1/endpoint") == "https://api.example.com/v1/endpoint"

    def test_handle_multiple_slashes(self):
        """Test handling of multiple consecutive slashes in URI and path."""
        assert build_uri("https://api.example.com///", "///v1/endpoint") == "https://api.example.com/v1/endpoint"


class LockedTaskTest:
    """A task lock that never expires strands every later run, so the timeout is load-bearing."""

    def test_lock_is_always_acquired_with_an_expiry(self, mocker):
        mock_lock = mocker.patch("core.utils.cache.lock")

        @locked_task(key="repo-access")
        def sync():
            return "ran"

        assert sync() == "ran"
        timeout = mock_lock.call_args.kwargs["timeout"]
        assert timeout == DEFAULT_LOCK_TIMEOUT
        assert timeout is not None and timeout > 0

    async def test_async_lock_is_always_acquired_with_an_expiry(self, mocker):
        mock_lock = mocker.patch("core.utils.cache.lock")

        @locked_task(key="repo-access")
        async def sync():
            return "ran"

        assert await sync() == "ran"
        assert mock_lock.call_args.kwargs["timeout"] == DEFAULT_LOCK_TIMEOUT

    def test_explicit_timeout_is_forwarded(self, mocker):
        mock_lock = mocker.patch("core.utils.cache.lock")

        @locked_task(key="repo-access", timeout=120)
        def sync():
            return "ran"

        sync()
        assert mock_lock.call_args.kwargs["timeout"] == 120

    @pytest.mark.parametrize("timeout", [None, 0, -1])
    def test_non_expiring_timeout_is_rejected(self, timeout):
        with pytest.raises(ValueError, match="positive number of seconds"):
            locked_task(key="repo-access", timeout=timeout)

    def test_task_is_skipped_while_the_lock_is_held(self, mocker):
        mock_lock = mocker.patch("core.utils.cache.lock")
        mock_lock.return_value.__enter__.side_effect = LockError("held")
        calls = []

        @locked_task(key="repo-access")
        def sync():
            calls.append(1)

        assert sync() is None
        assert calls == []

    @pytest.mark.parametrize(
        ("args", "kwargs"), [(("group/proj",), {}), ((), {"repo_id": "group/proj"})], ids=["positional", "keyword"]
    )
    def test_named_key_field_resolves_however_the_caller_passed_it(self, mocker, args, kwargs):
        mock_lock = mocker.patch("core.utils.cache.lock")

        @locked_task(key="{repo_id}")
        def consolidate(repo_id: str):
            return "ran"

        assert consolidate(*args, **kwargs) == "ran"
        assert mock_lock.call_args.args[0] == "consolidate:group/proj"

    async def test_named_key_field_resolves_for_an_async_task(self, mocker):
        mock_lock = mocker.patch("core.utils.cache.lock")

        @locked_task(key="{repo_id}")
        async def consolidate(repo_id: str):
            return "ran"

        assert await consolidate("group/proj") == "ran"
        assert mock_lock.call_args.args[0] == "consolidate:group/proj"

    def test_expiry_mid_run_is_reported_as_an_error(self, mocker):
        """Release raises when the lock expired under a live holder — a distinct failure from a skip."""
        mock_lock = mocker.patch("core.utils.cache.lock")
        mock_lock.return_value.__exit__.side_effect = LockNotOwnedError("expired")
        logger = mocker.patch("core.utils.logger")
        calls = []

        @locked_task(key="repo-access")
        def sync():
            calls.append(1)

        assert sync() is None
        assert calls == [1]
        assert logger.error.called
        assert not logger.warning.called
