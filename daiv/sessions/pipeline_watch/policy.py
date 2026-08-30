"""The watch's on/off switch and attempt cap. No I/O beyond the hour-cached repo config read."""

from __future__ import annotations

from asgiref.sync import sync_to_async

from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings


class WatchPolicy:
    """What a repository is allowed to do with the watch.

    The site switch is a ceiling in both directions: a repository can turn the watch off but never
    turn one on that the operator disabled, and can lower the attempt cap but never raise it.
    """

    def __init__(self, *, enabled: bool, max_attempts: int) -> None:
        self.enabled = enabled
        self.max_attempts = max_attempts

    @classmethod
    def enabled_for(cls, config: RepositoryConfig) -> bool:
        return bool(config.pipeline_watch.enabled and site_settings.pipeline_watch_enabled)

    @classmethod
    def from_config(cls, config: RepositoryConfig) -> WatchPolicy:
        return cls(
            enabled=cls.enabled_for(config),
            max_attempts=min(config.pipeline_watch.max_attempts, site_settings.pipeline_watch_max_attempts),
        )

    @classmethod
    async def afor_repo(cls, repo_id: str) -> WatchPolicy:
        return cls.from_config(await sync_to_async(RepositoryConfig.get_config)(repo_id))
