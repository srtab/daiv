from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from asgiref.sync import sync_to_async

from codebase.base import RepoAccessLevel
from codebase.conf import settings
from codebase.models import RepositoryAccess, RepositoryAccessSyncState, RepositoryCatalog

if TYPE_CHECKING:
    from collections.abc import Iterable

    from accounts.models import User

logger = logging.getLogger("daiv.codebase")

REPO_ACCESS_DENIED_MESSAGE = "Repository not found or not accessible."

# Enqueue a backstop sync when no sync has *started* within this window — covers a dead
# crontask scheduler well before the per-repo hard TTL starts denying access.
_BACKSTOP_STALENESS = timedelta(hours=1)

# The backstop only needs to detect a scheduler that has been dead for an hour, so probing the
# sync-state row on every authorization check is wildly over-sampled. This cache marker collapses
# the probe to at most once per minute process-wide, keeping it off the hot path.
_BACKSTOP_PROBE_KEY = "repo-access:backstop-probe"
_BACKSTOP_PROBE_INTERVAL = 60


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


def _fresh_rows(uid: str):
    """Fresh (within-hard-TTL) access rows for ``uid`` on the configured provider.

    Centralizes the provider + identity + freshness scoping shared by every authorization
    query so no call site can drift on the security-load-bearing freshness filter.
    """
    return RepositoryAccess.objects.fresh().filter(provider=_provider(), uid=uid)


def _maybe_enqueue_backstop() -> None:
    """Enqueue a backstop sync when the scheduler looks dead.

    Keyed on ``last_started_at`` (scheduler liveness), not ``last_success_at``: a single
    persistently-failing repository never lets ``last_success_at`` advance, and gating on it
    would flood the task queue with an enqueue on every authorization check. A running
    scheduler advances ``last_started_at`` every cycle even when some repos fail, so this
    only fires when no sync has *started* recently. The task's lock dedupes concurrent
    triggers.

    The sync-state row is probed at most once per ``_BACKSTOP_PROBE_INTERVAL`` (cache marker)
    to keep this off the per-request hot path.
    """
    if not cache.add(_BACKSTOP_PROBE_KEY, 1, timeout=_BACKSTOP_PROBE_INTERVAL):
        return
    state = RepositoryAccessSyncState.objects.filter(pk=RepositoryAccessSyncState.SINGLETON_PK).first()
    last_started = state.last_started_at if state else None
    now = timezone.now()
    if last_started is None or now - last_started > _BACKSTOP_STALENESS:
        _enqueue_sync()


def _resolve_uid(user: User) -> str | None:
    """Probe the backstop, then resolve ``user`` to their platform uid (``None`` if unlinked).

    Groups the two steps every non-admin authorization entry point shares, so the
    security-load-bearing "keep the sync alive, then map to a platform identity" ordering
    lives in one place and cannot drift between call sites.
    """
    _maybe_enqueue_backstop()
    return _identity(user)


def get_access_level(user: User, repo_id: str) -> RepoAccessLevel | None:
    """Effective access tier of ``user`` on ``repo_id``. Admins always hold WRITE.

    Freshness is enforced per repository: a row is trusted only while its own ``synced_at``
    is within the hard TTL, so a repo whose sync keeps failing eventually denies access to
    that repo alone without affecting repos that are still syncing cleanly.
    """
    if user.is_admin:
        return RepoAccessLevel.WRITE
    uid = _resolve_uid(user)
    if uid is None:
        return None
    row = _fresh_rows(uid).filter(repo_id=repo_id).only("access_level").first()
    return RepoAccessLevel(row.access_level) if row else None


def can_view(user: User, repo_id: str) -> bool:
    """Whether ``user`` holds at least READ access on ``repo_id``."""
    return get_access_level(user, repo_id) is not None


def assert_can_run(user: User, repo_ids: Iterable[str]) -> None:
    """Require WRITE access on every repo (fresh within the hard TTL).

    Raises:
        RepositoryAccessDenied: carrying the denied repo ids.
    """
    repo_ids = list(repo_ids)
    if user.is_admin:
        return
    uid = _resolve_uid(user)
    if uid is None:
        raise RepositoryAccessDenied(repo_ids)
    writable = set(
        _fresh_rows(uid)
        .filter(repo_id__in=repo_ids, access_level=RepoAccessLevel.WRITE)
        .values_list("repo_id", flat=True)
    )
    denied = [repo_id for repo_id in repo_ids if repo_id not in writable]
    if denied:
        raise RepositoryAccessDenied(denied)


def viewable_repo_ids(user: User, repo_ids: Iterable[str]) -> set[str]:
    """Subset of ``repo_ids`` on which the user holds at least READ access (fresh within the hard TTL)."""
    repo_ids = list(repo_ids)
    if user.is_admin:
        return set(repo_ids)
    uid = _resolve_uid(user)
    if uid is None:
        return set()
    return set(_fresh_rows(uid).filter(repo_id__in=repo_ids).values_list("repo_id", flat=True))


def search_viewable_repositories(
    user: User, *, search: str | None = None, topics: list[str] | None = None, limit: int
) -> list[RepositoryCatalog]:
    """Repositories the user may view (READ+), served from the local ``RepositoryCatalog`` mirror.

    Members are restricted to repos with a fresh ``RepositoryAccess`` row for their platform uid;
    admins see the whole fresh catalog. Results are ordered by ``slug`` and capped at ``limit``.
    ``topics`` is AND-matched in Python because the SQLite test backend does not support the
    ``JSONField __contains`` lookup used on Postgres. The match streams slug-ordered rows and stops
    once ``limit`` are found, so it never materializes more of the catalog than the window needs —
    important for an admin, whose unfiltered candidate set is the entire fresh catalog.
    """
    _maybe_enqueue_backstop()
    rows = RepositoryCatalog.objects.fresh().filter(provider=_provider())
    if not user.is_admin:
        uid = _identity(user)
        if uid is None:
            return []
        rows = rows.filter(slug__in=_fresh_rows(uid).values_list("repo_id", flat=True))
    if search:
        rows = rows.filter(Q(slug__icontains=search) | Q(name__icontains=search))
    rows = rows.order_by("slug")
    if topics:
        wanted = set(topics)
        matched: list[RepositoryCatalog] = []
        for row in rows.iterator():
            if wanted.issubset(set(row.topics)):
                matched.append(row)
                if len(matched) >= limit:
                    break
        return matched
    return list(rows[:limit])


# Only the wrappers with async call sites are exported; add more via ``sync_to_async`` as needed.
aassert_can_run = sync_to_async(assert_can_run)
asearch_viewable_repositories = sync_to_async(search_viewable_repositories)
