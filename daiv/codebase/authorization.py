from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

from asgiref.sync import sync_to_async

from codebase.base import RepoAccessLevel
from codebase.conf import settings
from codebase.models import RepositoryAccess, RepositoryAccessSyncState

if TYPE_CHECKING:
    from collections.abc import Iterable

    from accounts.models import User
    from codebase.base import Repository

logger = logging.getLogger("daiv.codebase")

REPO_ACCESS_DENIED_MESSAGE = "Repository not found or not accessible."

# Enqueue a backstop sync when the last successful sync is older than this — covers a dead
# crontask scheduler well before the hard TTL starts denying access.
_BACKSTOP_STALENESS = timedelta(hours=1)


class RepositoryAccessDenied(Exception):  # noqa: N818
    """The user lacks the required access level on one or more repositories."""

    def __init__(self, repo_ids: list[str]):
        self.repo_ids = repo_ids
        super().__init__(REPO_ACCESS_DENIED_MESSAGE)


def _provider() -> str:
    return settings.CLIENT.value


def _identity(user: User) -> str | None:
    """Return the user's verified platform uid, or None when no OAuth link exists."""
    from allauth.socialaccount.models import SocialAccount

    account = SocialAccount.objects.filter(user=user, provider=_provider()).only("uid").first()
    return account.uid if account else None


def _enqueue_sync() -> None:
    from codebase.tasks import sync_repository_access_cron_task

    try:
        sync_repository_access_cron_task.enqueue()
    except Exception:
        logger.exception("Failed to enqueue repository access sync")


def _sync_is_usable() -> bool:
    """Whether synced rows may be trusted (fail-closed beyond the hard TTL).

    Enqueues a backstop sync when data is missing or stale; the task's lock dedupes
    concurrent triggers.
    """
    state = RepositoryAccessSyncState.objects.first()
    last_success = state.last_success_at if state else None
    now = timezone.now()
    if last_success is None or now - last_success > _BACKSTOP_STALENESS:
        _enqueue_sync()
    return last_success is not None and now - last_success <= timedelta(hours=settings.REPO_ACCESS_HARD_TTL_HOURS)


def get_access_level(user: User, repo_id: str) -> RepoAccessLevel | None:
    """Effective access tier of ``user`` on ``repo_id``. Admins always hold WRITE."""
    if user.is_admin:
        return RepoAccessLevel.WRITE
    if not _sync_is_usable():
        return None
    uid = _identity(user)
    if uid is None:
        return None
    row = RepositoryAccess.objects.filter(provider=_provider(), uid=uid, repo_id=repo_id).only("access_level").first()
    return RepoAccessLevel(row.access_level) if row else None


def can_view(user: User, repo_id: str) -> bool:
    """Whether ``user`` holds at least READ access on ``repo_id``."""
    return get_access_level(user, repo_id) is not None


def assert_can_run(user: User, repo_ids: Iterable[str]) -> None:
    """Require WRITE access on every repo.

    Raises:
        RepositoryAccessDenied: carrying the denied repo ids.
    """
    repo_ids = list(repo_ids)
    if user.is_admin:
        return
    if not _sync_is_usable():
        raise RepositoryAccessDenied(repo_ids)
    uid = _identity(user)
    if uid is None:
        raise RepositoryAccessDenied(repo_ids)
    writable = set(
        RepositoryAccess.objects.filter(
            provider=_provider(), uid=uid, repo_id__in=repo_ids, access_level=RepoAccessLevel.WRITE
        ).values_list("repo_id", flat=True)
    )
    denied = [repo_id for repo_id in repo_ids if repo_id not in writable]
    if denied:
        raise RepositoryAccessDenied(denied)


def viewable_repo_ids(user: User, repo_ids: Iterable[str]) -> set[str]:
    """Subset of ``repo_ids`` on which the user holds at least READ access."""
    repo_ids = list(repo_ids)
    if user.is_admin:
        return set(repo_ids)
    if not _sync_is_usable():
        return set()
    uid = _identity(user)
    if uid is None:
        return set()
    return set(
        RepositoryAccess.objects.filter(provider=_provider(), uid=uid, repo_id__in=repo_ids).values_list(
            "repo_id", flat=True
        )
    )


def filter_viewable(user: User, repositories: list[Repository]) -> list[Repository]:
    """Filter a platform repository list down to the entries the user can view."""
    viewable = viewable_repo_ids(user, [repo.slug for repo in repositories])
    return [repo for repo in repositories if repo.slug in viewable]


aget_access_level = sync_to_async(get_access_level)
acan_view = sync_to_async(can_view)
aassert_can_run = sync_to_async(assert_can_run)
aviewable_repo_ids = sync_to_async(viewable_repo_ids)
afilter_viewable = sync_to_async(filter_viewable)
