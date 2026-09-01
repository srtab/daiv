from unittest.mock import patch

from sessions.pipeline_watch.policy import WatchPolicy

from codebase.repo_config import RepositoryConfig
from core.models import SiteConfiguration


def test_the_attempt_cap_is_clamped_to_the_site_value(site_setting):
    site_setting("pipeline_watch_max_attempts", 2)
    assert WatchPolicy(RepositoryConfig(**{"pipeline_watch": {"max_attempts": 10}})).max_attempts == 2
    assert WatchPolicy(RepositoryConfig(**{"pipeline_watch": {"max_attempts": 1}})).max_attempts == 1


def test_a_repo_cannot_enable_a_watch_the_operator_turned_off(site_setting):
    site_setting("pipeline_watch_enabled", False)
    assert WatchPolicy(RepositoryConfig(**{"pipeline_watch": {"enabled": True}})).enabled is False

    site_setting("pipeline_watch_enabled", True)
    assert WatchPolicy(RepositoryConfig(**{"pipeline_watch": {"enabled": False}})).enabled is False
    assert WatchPolicy(RepositoryConfig()).enabled is True


def test_reading_enabled_reaches_site_settings_for_a_repo_that_wants_the_watch():
    """The positive control for the two guards below.

    Without it they cannot distinguish "the read was skipped" from "the read was unreachable" — a
    ``monkeypatch.setattr`` on the ``site_settings`` singleton anywhere earlier leaves the value in
    its ``__dict__``, after which no read reaches ``get_cached`` and an ``assert_not_called`` guard
    passes against any implementation at all.
    """
    config = RepositoryConfig(**{"pipeline_watch": {"enabled": True}})

    with patch.object(SiteConfiguration, "get_cached") as get_cached:
        WatchPolicy.enabled_for(config)

    assert get_cached.call_count >= 1


def test_enabled_short_circuits_without_reading_site_settings():
    """A repo with the watch off must short-circuit before ``site_settings`` is ever read: the two
    webhook accept paths run this synchronously on the event loop, and each site-settings read
    blocks on a thread hop. Asserting the return value alone would not catch a regression that
    reads ``site_settings`` and then discards the result.
    """
    config = RepositoryConfig(**{"pipeline_watch": {"enabled": False, "max_attempts": 1}})

    with patch.object(SiteConfiguration, "get_cached") as get_cached:
        assert WatchPolicy.enabled_for(config) is False
        assert WatchPolicy(config).enabled is False

    get_cached.assert_not_called()


def test_asking_only_for_enabled_never_costs_the_attempt_cap():
    """``aarm`` reads ``enabled`` and never ``max_attempts``; ``_aact`` reads only the cap. Computing
    both up front made each path pay the other's blocking read, which is the regression that shipped.
    """
    config = RepositoryConfig(**{"pipeline_watch": {"enabled": True, "max_attempts": 10}})

    with patch.object(SiteConfiguration, "get_cached") as get_cached:
        assert WatchPolicy(config).enabled is True

    assert get_cached.call_count == 1


async def test_it_resolves_a_repo_id_through_the_config_cache(monkeypatch, site_setting):
    """``afor_repo`` is the seam a caller holding only a repo id uses; it must apply the same
    ceiling the constructor does rather than returning the repo's raw values."""
    site_setting("pipeline_watch_max_attempts", 2)
    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config",
        lambda *_a, **_kw: RepositoryConfig(**{"pipeline_watch": {"max_attempts": 10}}),
    )

    policy = await WatchPolicy.afor_repo("group/repo")

    assert policy.max_attempts == 2
