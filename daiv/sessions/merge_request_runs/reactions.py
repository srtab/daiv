"""Keep one GitLab review-in-progress award while any tracked review on that MR is running.

Membership changes are atomic and do not wait for remote I/O. A short lease serializes the
award operations. Persist deletion intent before deleting an exact award so a successor
does not reuse that award if the lease expires in flight. A creation with no confirmed
receipt can leave an award behind; we never assume ownership of an observed award.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, cast

from redis.exceptions import LockNotOwnedError, WatchError

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.conf import settings as codebase_settings
from core.redis import redis_connections
from sessions.models import Run, RunStatus, SessionOrigin
from slash_commands.parser import parse_bare_slash_command

if TYPE_CHECKING:
    from collections.abc import Collection

    from redis import Redis
    from redis.lock import Lock


logger = logging.getLogger("daiv.sessions")
_KEY_PREFIX = "daiv:review-reactions"
_CREATING = "creating"
_EXISTING = "existing"
_DELETING = "deleting:"
_LOCK_SECONDS = 60


class ReviewReactions:
    """Best-effort running review marker for an explicitly targeted GitLab merge request."""

    def __init__(self, redis: Redis, namespace: str, group: str):
        self.redis = redis
        self.group = group
        self.project_id, _, iid = group.partition(":")
        self.merge_request_iid = int(iid)
        self.groups = f"{namespace}:groups"
        self.reverse = f"{namespace}:run-group"
        self.members = f"{namespace}:{group}:members"
        self.award = f"{namespace}:{group}:award"
        self.lock_name = f"{namespace}:{group}:lock"

    @classmethod
    def start(cls, run_id: str, repo_id: str, merge_request_iid: int) -> None:
        """Register an eligible run once. A later reconcile retries platform work that could not finish."""
        try:
            if not (namespace := cls._namespace()):
                return
            prompt = (
                Run.objects
                .filter(pk=run_id, trigger_type=SessionOrigin.API_JOB, runs_alone=True)
                .exclude(status__in=RunStatus.terminal())
                .values_list("prompt", flat=True)
                .first()
            )
            command = parse_bare_slash_command(prompt or "")
            if command is None or command.command != "code-review":
                return
            client = RepoClient.create_instance()
            project_id = client.get_repository_id(repo_id)
            group = cls(redis_connections.sync_client(), namespace, f"{project_id}:{merge_request_iid}")
            with group.redis.pipeline() as pipe:
                pipe.sadd(group.groups, group.group)
                pipe.sadd(group.members, run_id)
                pipe.hset(group.reverse, run_id, group.group)
                pipe.execute()
            group._sync(client)
        except Exception:
            logger.exception("Could not set the running review reaction for run %s", run_id)

    @classmethod
    def finish(cls, run_id: str) -> None:
        """Unregister a finished run. Its mapping also works after a repository rename."""
        try:
            if not (namespace := cls._namespace()):
                return
            redis = redis_connections.sync_client()
            if key := cast("str | None", redis.hget(f"{namespace}:run-group", run_id)):
                group = cls(redis, namespace, key)
                group._discard({run_id})
                group._sync(RepoClient.create_instance())
        except Exception:
            logger.exception("Could not clear the running review reaction for run %s", run_id)

    @classmethod
    def reconcile(cls) -> None:
        """Retry indexed groups, including runs whose finish signal did not arrive."""
        try:
            if not (namespace := cls._namespace()):
                return
            redis = redis_connections.sync_client()
            for key in redis.sscan_iter(f"{namespace}:groups"):
                try:
                    cls(redis, namespace, key)._sync(RepoClient.create_instance())
                except Exception:
                    logger.exception("Could not reconcile the running review reaction for %s", key)
        except Exception:
            logger.exception("Could not scan the running review reactions")

    @staticmethod
    def _namespace() -> str | None:
        if codebase_settings.CLIENT != GitPlatform.GITLAB or not redis_connections.configured:
            return None
        # The default instance matches GitLabClient. Stable numeric IDs are unique only within it.
        url = str(codebase_settings.GITLAB_URL or "https://gitlab.com").rstrip("/")
        return f"{_KEY_PREFIX}:{hashlib.sha256(url.encode()).hexdigest()}"

    def _discard(self, run_ids: Collection[str]) -> None:
        if run_ids:
            with self.redis.pipeline() as pipe:
                pipe.srem(self.members, *run_ids)
                pipe.hdel(self.reverse, *run_ids)
                pipe.execute()

    def _prune(self) -> None:
        members = cast("set[str]", self.redis.smembers(self.members))
        if members:
            live = (
                Run.objects.filter(pk__in=members).exclude(status__in=RunStatus.terminal()).values_list("pk", flat=True)
            )
            self._discard(members - {str(run_id) for run_id in live})

    def _sync(self, client: RepoClient) -> None:
        lock = self.redis.lock(self.lock_name, timeout=_LOCK_SECONDS, blocking=False)
        if not lock.acquire():
            return
        try:
            self._prune()
            self._sync_award(client, lock)
        finally:
            try:
                lock.release()
            except LockNotOwnedError:
                logger.warning("Running review reaction lease expired for %s", self.group)

    def _sync_award(self, client: RepoClient, lock: Lock) -> None:
        award = cast("str | None", self.redis.get(self.award))
        if award and award.startswith(_DELETING):
            if not self._delete_award(client, lock, int(award.removeprefix(_DELETING)), pending=True):
                return
            award = None

        if not self.redis.exists(self.members):
            if not self._finish_group(client, lock, award):
                return
            award = None
        if not self.redis.exists(self.members) or (award is not None and award != _CREATING):
            return
        # A run can finish while a pending deletion was in flight. Persist the intent
        # only if a tracked run is still active before this idempotent POST.
        self._prune()
        if not self._store_award(lock, _CREATING, needs_members=True):
            return
        award_id = client.create_merge_request_emoji(self.project_id, self.merge_request_iid, Emoji.EYES)
        if award_id is None and award == _CREATING:
            logger.warning("Preserving an existing review reaction after an unconfirmed creation for %s", self.group)
        known_award = str(award_id) if award_id is not None else _EXISTING
        if self._store_award(lock, known_award) and not self.redis.exists(self.members):
            self._finish_group(client, lock, known_award)

    def _finish_group(self, client: RepoClient, lock: Lock, award: str | None) -> bool:
        if award not in (None, _CREATING, _EXISTING) and not self._delete_award(client, lock, int(award)):
            return False
        if self._store_award(lock, None, needs_members=False, unindex=True) and award == _CREATING:
            logger.warning("Discarding unconfirmed running review reaction for %s", self.group)
        # Membership can change under the lease. A run that joined during DELETE needs a new award.
        return True

    def _delete_award(self, client: RepoClient, lock: Lock, award_id: int, *, pending: bool = False) -> bool:
        if not pending and not self._store_award(lock, f"{_DELETING}{award_id}", needs_members=False):
            return False
        if not lock.owned():
            return False
        client.delete_merge_request_emoji(self.project_id, self.merge_request_iid, award_id)
        return self._store_award(lock, None)

    def _store_award(
        self, lock: Lock, value: str | None, *, needs_members: bool | None = None, unindex: bool = False
    ) -> bool:
        """Fence writes by lease ownership and, where required, the latest membership."""
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(lock.name)
                if not lock.owned():
                    return False
                if needs_members is not None:
                    pipe.watch(self.members)
                    if bool(pipe.exists(self.members)) != needs_members:
                        return False
                pipe.multi()
                if value is None:
                    pipe.delete(self.award)
                else:
                    pipe.set(self.award, value)
                if unindex:
                    pipe.srem(self.groups, self.group)
                pipe.execute()
            except WatchError:
                return False
        return True
