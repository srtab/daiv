"""The watch's on/off switch and attempt cap.

No I/O of its own: the caller hands in a ``RepositoryConfig``, and only ``afor_repo`` resolves one
— through the hour-cached ``.daiv.yml`` read, which on a cache miss does reach the platform.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from automation.agent.agent_settings import resolve_features
from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings

if TYPE_CHECKING:
    from automation.agent.agent_settings import Features


class WatchPolicy:
    """What a repository is allowed to do with the watch.

    The site switch is a ceiling in both directions: a repository can turn the watch off but never
    turn one on that the operator disabled, and can lower the attempt cap but never raise it.

    The site is read on first use, once: ``snapshot()`` blocks the event loop on a thread hop, and
    ``enabled`` checks the repository first, so a repository that turned the watch off never pays for it.
    """

    def __init__(self, config: RepositoryConfig) -> None:
        self._config = config

    @functools.cached_property
    def _features(self) -> Features:
        return resolve_features(site=site_settings.snapshot(), repo=self._config)

    @functools.cached_property
    def enabled(self) -> bool:
        return bool(self._config.pipeline_watch.enabled and self._features.pipeline_watch)

    @functools.cached_property
    def max_attempts(self) -> int:
        return self._features.pipeline_watch_max_attempts

    @classmethod
    def enabled_for(cls, config: RepositoryConfig) -> bool:
        return cls(config).enabled

    @classmethod
    async def afor_repo(cls, repo_id: str) -> WatchPolicy:
        return cls(await sync_to_async(RepositoryConfig.get_config)(repo_id))
