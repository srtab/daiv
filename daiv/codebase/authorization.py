from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
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
    from collections.abc import Iterable, Sequence

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
    if row is None:
        return None
    try:
        return RepoAccessLevel(row.access_level)
    except ValueError:
        # An unknown tier (bad migration, a platform level this enum hasn't learned) must
        # deny rather than 500 the caller — this is a security decision, so fail closed.
        logger.warning("Unknown access_level %r on %s for uid %s; denying", row.access_level, repo_id, uid)
        return None


def can_view(user: User, repo_id: str) -> bool:
    """Whether ``user`` holds at least READ access on ``repo_id``."""
    return get_access_level(user, repo_id) is not None


def assert_can_run(user: User, repo_ids: Iterable[str]) -> None:
    """Require WRITE access on every repo (fresh within the hard TTL).

    Raises:
        RepositoryAccessDenied: carrying the denied repo ids.
    """
    repo_ids = list(repo_ids)
    writable = runnable_repo_ids(user, repo_ids)
    denied = [repo_id for repo_id in repo_ids if repo_id not in writable]
    if denied:
        raise RepositoryAccessDenied(denied)


def runnable_repo_ids(user: User, repo_ids: Iterable[str]) -> set[str]:
    """Subset of ``repo_ids`` on which the user holds WRITE access (fresh within the hard TTL); one query.

    Admins may run on any repository in the catalog, stale rows included so a dead sync doesn't lock them out, and on
    any repository at all while the catalog is still empty.
    """
    repo_ids = list(repo_ids)
    if user.is_admin:
        return _catalogued_repo_ids(repo_ids)
    uid = _resolve_uid(user)
    if uid is None:
        return set()
    return set(
        _fresh_rows(uid)
        .filter(repo_id__in=repo_ids, access_level=RepoAccessLevel.WRITE)
        .values_list("repo_id", flat=True)
    )


def assert_catalogued(repo_ids: Iterable[str]) -> None:
    """Require every repo to be in DAIV's catalog, the repositories DAIV's bot can reach; any repo while the catalog is
    still empty. For a caller whose access the platform itself proved, such as a CI identity token's pipeline.

    Raises:
        RepositoryAccessDenied: carrying the uncatalogued repo ids.
    """
    repo_ids = list(repo_ids)
    found = _catalogued_repo_ids(repo_ids)
    if denied := [repo_id for repo_id in repo_ids if repo_id not in found]:
        raise RepositoryAccessDenied(denied)


def _catalogued_repo_ids(repo_ids: list[str]) -> set[str]:
    catalog = RepositoryCatalog.objects.filter(provider=_provider())
    found = set(catalog.filter(slug__in=repo_ids).values_list("slug", flat=True))
    if not found and not catalog.exists():
        return set(repo_ids)
    return found


def viewable_repo_ids(user: User, repo_ids: Iterable[str]) -> set[str]:
    """Subset of ``repo_ids`` on which the user holds at least READ access (fresh within the hard TTL)."""
    repo_ids = list(repo_ids)
    if user.is_admin:
        return set(repo_ids)
    uid = _resolve_uid(user)
    if uid is None:
        return set()
    return set(_fresh_rows(uid).filter(repo_id__in=repo_ids).values_list("repo_id", flat=True))


def viewable_repo_ids_subquery(user: User):
    """A lazy ``repo_id`` values-queryset of the repos the user can read (fresh READ+).

    Intended for ``repo_id__in=<subquery>`` filters over large tables (e.g. ``Activity``)
    so the id set is never materialized in Python. Returns an empty values-queryset when
    the user has no linked platform identity.

    Callers MUST handle admins (who are unrestricted) before calling this: an admin with
    no OAuth link would otherwise resolve to an empty set.
    """
    uid = _resolve_uid(user)
    if uid is None:
        return RepositoryAccess.objects.none().values_list("repo_id", flat=True)
    return _fresh_rows(uid).values_list("repo_id", flat=True)


def can_run(user: User, repo_id: str) -> bool:
    """Whether ``user`` may run on ``repo_id``: see :func:`runnable_repo_ids`."""
    return repo_id in runnable_repo_ids(user, [repo_id])


def _listable_rows(user: User, *, min_level: RepoAccessLevel):
    """Fresh catalog rows ``user`` holds at least ``min_level`` on; admins get the whole fresh catalog.

    ``None`` when a member has no linked platform identity.
    """
    _maybe_enqueue_backstop()
    rows = RepositoryCatalog.objects.fresh().filter(provider=_provider())
    if user.is_admin:
        return rows
    uid = _identity(user)
    if uid is None:
        return None
    access = _fresh_rows(uid)
    if min_level == RepoAccessLevel.WRITE:
        access = access.filter(access_level=RepoAccessLevel.WRITE)
    return rows.filter(slug__in=access.values_list("repo_id", flat=True))


def _text_filtered(rows, search: str | None):
    if search:
        rows = rows.filter(Q(slug__icontains=search) | Q(name__icontains=search))
    return rows.order_by("slug")


def _row_matches(row: RepositoryCatalog, topics: Iterable[str] | None, language: str | None) -> bool:
    if topics and not set(topics).issubset(row.topics):
        return False
    return not language or row.matches_language(language)


def _search_catalog(
    user: User,
    *,
    min_level: RepoAccessLevel,
    search: str | None,
    topics: list[str] | None,
    language: str | None,
    limit: int,
    after_slug: str | None,
) -> list[RepositoryCatalog]:
    rows = _listable_rows(user, min_level=min_level)
    if rows is None:
        return []
    if after_slug:
        rows = rows.filter(slug__gt=after_slug)
    rows = _text_filtered(rows, search)
    if not topics and not language:
        return list(rows[:limit])
    matched: list[RepositoryCatalog] = []
    # Django's default chunk_size is 2000; paging a filtered catalog would re-pay that full fetch
    # per page even though the loop stops at ``limit``.
    for row in rows.iterator(chunk_size=max(limit * 4, 50)):
        if _row_matches(row, topics, language):
            matched.append(row)
            if len(matched) >= limit:
                break
    return matched


def search_viewable_repositories(
    user: User,
    *,
    search: str | None = None,
    topics: list[str] | None = None,
    language: str | None = None,
    limit: int,
    after_slug: str | None = None,
) -> list[RepositoryCatalog]:
    """Repositories the user may view (READ+), served from the local ``RepositoryCatalog`` mirror.

    Members are restricted to repos with a fresh ``RepositoryAccess`` row for their platform uid;
    admins see the whole fresh catalog. Results are ordered by ``slug`` and capped at ``limit``.
    ``after_slug`` resumes strictly after that slug (keyset pagination); ``slug`` is unique per
    provider, so a page boundary cannot repeat or skip a row the way an ``OFFSET`` can. Rows
    synced in or aged past the freshness TTL between pages are still missed — ``fresh()`` is
    re-evaluated per call, so a walk is not a snapshot.
    ``topics`` (AND-matched) and ``language`` (``LANGUAGE_MATCH_MIN_SHARE`` or more of the code) are
    matched in Python because the SQLite test backend does not support the ``JSONField __contains``
    lookup used on Postgres. The match streams slug-ordered rows and stops once ``limit`` are found,
    so it never materializes more of the catalog than the window needs.
    """
    return _search_catalog(
        user,
        min_level=RepoAccessLevel.READ,
        search=search,
        topics=topics,
        language=language,
        limit=limit,
        after_slug=after_slug,
    )


def search_runnable_repositories(
    user: User,
    *,
    search: str | None = None,
    topics: list[str] | None = None,
    language: str | None = None,
    limit: int,
    after_slug: str | None = None,
) -> list[RepositoryCatalog]:
    """:func:`search_viewable_repositories` narrowed to repositories the user can run on (WRITE)."""
    return _search_catalog(
        user,
        min_level=RepoAccessLevel.WRITE,
        search=search,
        topics=topics,
        language=language,
        limit=limit,
        after_slug=after_slug,
    )


@dataclass(frozen=True)
class RepositoryFacets:
    """Languages and topics among the runnable repositories, each with how many repositories it narrows to."""

    languages: list[tuple[str, int]]
    topics: list[tuple[str, int]]


def _ranked(counts: Counter[str]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def runnable_repository_facets(
    user: User, *, topic: str | None = None, language: str | None = None
) -> RepositoryFacets:
    """Each dimension's counts honour the other dimension's selection, never the typed search."""
    rows = _listable_rows(user, min_level=RepoAccessLevel.WRITE)
    if rows is None:
        return RepositoryFacets(languages=[], topics=[])
    languages: Counter[str] = Counter()
    topics: Counter[str] = Counter()
    for row in rows.only("slug", "topics", "languages").iterator():
        if _row_matches(row, [topic] if topic else None, None):
            languages.update(row.matched_languages())
        if _row_matches(row, None, language):
            topics.update(row.topics)
    return RepositoryFacets(languages=_ranked(languages), topics=_ranked(topics))


@dataclass(frozen=True)
class RepositoryMatches:
    """What a "select all" needs: the match count, the first unselected matches, and which selected slugs match."""

    total: int
    candidates: list[RepositoryCatalog]
    matching_selected: list[str]


def runnable_repository_matches(
    user: User,
    *,
    search: str | None = None,
    topic: str | None = None,
    language: str | None = None,
    selected: Sequence[str] = (),
    candidate_limit: int,
) -> RepositoryMatches:
    """Candidates include selected matches, so one unselected after the list loaded can still be re-added."""
    rows = _listable_rows(user, min_level=RepoAccessLevel.WRITE)
    if rows is None:
        return RepositoryMatches(total=0, candidates=[], matching_selected=[])
    rows = _text_filtered(rows, search)
    selected_set = set(selected)
    limit = candidate_limit + len(selected_set)
    if not topic and not language:
        return RepositoryMatches(
            total=rows.count(),
            candidates=list(rows[:limit]),
            matching_selected=list(rows.filter(slug__in=selected_set).values_list("slug", flat=True)),
        )
    total = 0
    candidates: list[RepositoryCatalog] = []
    matching_selected: list[str] = []
    for row in rows.only("slug", "default_branch", "topics", "languages").iterator():
        if not _row_matches(row, [topic] if topic else None, language):
            continue
        total += 1
        if row.slug in selected_set:
            matching_selected.append(row.slug)
        if len(candidates) < limit:
            candidates.append(row)
    return RepositoryMatches(total=total, candidates=candidates, matching_selected=matching_selected)


# Only the wrappers with async call sites are exported; add more via ``sync_to_async`` as needed.
aassert_can_run = sync_to_async(assert_can_run)
asearch_viewable_repositories = sync_to_async(search_viewable_repositories)
