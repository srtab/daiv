from sessions.pipeline_watch.policy import WatchPolicy

from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings


def test_the_attempt_cap_is_clamped_to_the_site_value(monkeypatch):
    monkeypatch.setattr(site_settings, "pipeline_watch_max_attempts", 2)
    assert WatchPolicy.from_config(RepositoryConfig(**{"pipeline_watch": {"max_attempts": 10}})).max_attempts == 2
    assert WatchPolicy.from_config(RepositoryConfig(**{"pipeline_watch": {"max_attempts": 1}})).max_attempts == 1


def test_a_repo_cannot_enable_a_watch_the_operator_turned_off(monkeypatch):
    monkeypatch.setattr(site_settings, "pipeline_watch_enabled", False)
    assert WatchPolicy.from_config(RepositoryConfig(**{"pipeline_watch": {"enabled": True}})).enabled is False

    monkeypatch.setattr(site_settings, "pipeline_watch_enabled", True)
    assert WatchPolicy.from_config(RepositoryConfig(**{"pipeline_watch": {"enabled": False}})).enabled is False
    assert WatchPolicy.from_config(RepositoryConfig()).enabled is True


async def test_it_resolves_a_repo_id_through_the_config_cache(monkeypatch):
    """``afor_repo`` is the seam a caller holding only a repo id uses; it must apply the same
    ceiling as ``from_config`` rather than returning the repo's raw values."""
    monkeypatch.setattr(site_settings, "pipeline_watch_max_attempts", 2)
    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config",
        lambda *_a, **_kw: RepositoryConfig(**{"pipeline_watch": {"max_attempts": 10}}),
    )

    policy = await WatchPolicy.afor_repo("group/repo")

    assert policy.max_attempts == 2
