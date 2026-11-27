"""Offline review-reaction lifecycle, including Redis membership and lease races."""

from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import HttpUrl
from redis.exceptions import LockNotOwnedError, WatchError
from sessions.merge_request_runs import reactions
from sessions.merge_request_runs.reactions import ReviewReactions
from sessions.models import Run, RunStatus, Session, SessionOrigin

from codebase.base import GitPlatform
from codebase.clients.base import Emoji

if TYPE_CHECKING:
    from redis import Redis


class _Redis:
    """Synchronous stand-in for the few Redis commands the coordinator uses."""

    def __init__(self):
        self.data = {}
        self.revisions = defaultdict(int)
        self.locks = {}

    def pipeline(self):
        return _Pipeline(self)

    def lock(self, name, *, timeout, blocking):
        assert timeout > 0 and not blocking
        return _Lock(self, name)

    def _changed(self, key):
        self.revisions[key] += 1

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
        self._changed(key)

    def exists(self, key):
        return key in self.data

    def delete(self, key):
        if key in self.data:
            del self.data[key]
            self._changed(key)

    def sadd(self, key, *members):
        self.data.setdefault(key, set()).update(members)
        self._changed(key)

    def srem(self, key, *members):
        if group := self.data.get(key):
            group.difference_update(members)
            self._changed(key)
            if not group:
                self.delete(key)

    def smembers(self, key):
        return set(self.data.get(key, ()))

    def sscan_iter(self, key):
        return iter(self.smembers(key))

    def hset(self, key, item, value):
        self.data.setdefault(key, {})[item] = value
        self._changed(key)

    def hget(self, key, item):
        return self.data.get(key, {}).get(item)

    def hdel(self, key, *items):
        if index := self.data.get(key):
            for item in items:
                index.pop(item, None)
            self._changed(key)
            if not index:
                self.delete(key)


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.watched = {}
        self.commands = []
        self.buffering = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def watch(self, key):
        self.buffering = False
        self.watched[key] = self.redis.revisions[key]

    def multi(self):
        self.buffering = True

    def execute(self):
        if any(self.redis.revisions[key] != revision for key, revision in self.watched.items()):
            raise WatchError
        for command, args in self.commands:
            command(*args)

    def __getattr__(self, name):
        command = getattr(self.redis, name)
        if not self.buffering:
            return command

        def add(*args):
            self.commands.append((command, args))

        return add


class _Lock:
    def __init__(self, redis, name):
        self.redis = redis
        self.name = name

    def acquire(self):
        if self.name in self.redis.locks:
            return False
        self.redis.locks[self.name] = self
        self.redis._changed(self.name)  # noqa: SLF001 - fake Redis implementation
        return True

    def owned(self):
        return self.redis.locks.get(self.name) is self

    def release(self):
        if not self.owned():
            raise LockNotOwnedError
        del self.redis.locks[self.name]
        self.redis._changed(self.name)  # noqa: SLF001 - fake Redis implementation


@dataclass
class _Fixture:
    redis: _Redis
    client: Mock
    connections: SimpleNamespace

    def group(self):
        namespace = ReviewReactions._namespace()  # noqa: SLF001 - inspect tracker state
        assert namespace is not None
        return ReviewReactions(cast("Redis", self.redis), namespace, "42:7")


@pytest.fixture
def tracker(monkeypatch):
    redis = _Redis()
    connections = SimpleNamespace(configured=True, sync_client=Mock(return_value=redis))
    client = Mock()
    client.get_repository_id.return_value = 42
    client.create_merge_request_emoji.return_value = 101
    monkeypatch.setattr(reactions, "redis_connections", connections)
    monkeypatch.setattr(reactions.RepoClient, "create_instance", Mock(return_value=client))
    monkeypatch.setattr(reactions.codebase_settings, "CLIENT", GitPlatform.GITLAB)
    monkeypatch.setattr(reactions.codebase_settings, "GITLAB_URL", HttpUrl("https://gitlab.example"))
    return _Fixture(redis, client, connections)


def _run(**fields):
    return Run.objects.create(**{
        "session": Session.objects.create(
            thread_id=str(uuid4()), repo_id="group/project", origin=SessionOrigin.API_JOB
        ),
        "repo_id": "group/project",
        "status": RunStatus.RUNNING,
        "prompt": "/code-review",
        "trigger_type": SessionOrigin.API_JOB,
        "runs_alone": True,
        **fields,
    })


def _start(run, repo_id="group/project"):
    ReviewReactions.start(str(run.pk), repo_id, 7)


def _finish_row(run):
    Run.objects.filter(pk=run.pk).update(status=RunStatus.SUCCESSFUL)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fields",
    [
        {"prompt": "/code-review-later"},
        {"prompt": "Please run /code-review"},
        {"prompt": '/code-review "unmatched'},
        {"trigger_type": SessionOrigin.MCP_JOB},
        {"trigger_type": SessionOrigin.MR_WEBHOOK},
        {"runs_alone": False},
        {"status": RunStatus.WAITING_INPUT},
    ],
)
def test_ineligible_runs_leave_no_tracking_or_platform_calls(tracker, fields):
    _start(_run(**fields))
    assert not tracker.client.mock_calls
    assert not tracker.redis.data


@pytest.mark.django_db
@pytest.mark.parametrize("configured, platform", [(False, GitPlatform.GITLAB), (True, GitPlatform.GITHUB)])
def test_inactive_service_is_a_clean_no_op(tracker, monkeypatch, configured, platform):
    tracker.connections.configured = configured
    monkeypatch.setattr(reactions.codebase_settings, "CLIENT", platform)
    run = _run()
    _start(run)
    ReviewReactions.finish(str(run.pk))
    ReviewReactions.reconcile()
    tracker.connections.sync_client.assert_not_called()
    assert not tracker.client.mock_calls


@pytest.mark.django_db
def test_overlapping_aliases_keep_one_award_until_last_run(tracker):
    first, second = _run(prompt=" \t/CODE-REVIEW\nPlease review"), _run()
    _start(first)
    _start(first)
    _start(second, repo_id="renamed/project")
    group = tracker.group()
    assert tracker.redis.smembers(group.members) == {str(first.pk), str(second.pk)}
    tracker.client.create_merge_request_emoji.assert_called_once_with("42", 7, Emoji.EYES)

    _finish_row(first)
    ReviewReactions.finish(str(first.pk))
    ReviewReactions.finish(str(first.pk))
    tracker.client.delete_merge_request_emoji.assert_not_called()
    _finish_row(second)
    ReviewReactions.finish(str(second.pk))
    tracker.client.delete_merge_request_emoji.assert_called_once_with("42", 7, 101)
    assert not tracker.redis.data


@pytest.mark.django_db
def test_existing_same_bot_award_is_not_claimed_or_removed(tracker):
    tracker.client.create_merge_request_emoji.return_value = None
    run = _run()
    _start(run)
    ReviewReactions.reconcile()
    _finish_row(run)
    ReviewReactions.finish(str(run.pk))
    tracker.client.create_merge_request_emoji.assert_called_once_with("42", 7, Emoji.EYES)
    tracker.client.delete_merge_request_emoji.assert_not_called()
    assert not tracker.redis.data


@pytest.mark.django_db
def test_ambiguous_post_preserves_any_observed_award(tracker, caplog):
    platform_awards = set()

    def lose_receipt(*_args):
        if platform_awards:
            return None  # The retry proves there is already an award, but not how it got there.
        platform_awards.add(88)
        raise OSError("response lost")

    tracker.client.create_merge_request_emoji.side_effect = lose_receipt
    run = _run()
    _start(run)
    assert "Could not set the running review reaction" in caplog.text
    ReviewReactions.reconcile()
    ReviewReactions.reconcile()
    assert tracker.client.create_merge_request_emoji.call_count == 2
    assert "Preserving an existing review reaction after an unconfirmed creation" in caplog.text
    _finish_row(run)
    ReviewReactions.reconcile()
    tracker.client.delete_merge_request_emoji.assert_not_called()
    assert not tracker.redis.data


@pytest.mark.django_db
def test_failed_creation_retries_through_idempotent_post(tracker):
    tracker.client.create_merge_request_emoji.side_effect = [OSError("request lost"), 101]
    run = _run()
    _start(run)
    ReviewReactions.reconcile()
    assert tracker.client.create_merge_request_emoji.call_count == 2
    _finish_row(run)
    ReviewReactions.finish(str(run.pk))
    tracker.client.delete_merge_request_emoji.assert_called_once_with("42", 7, 101)


@pytest.mark.django_db
def test_failed_delete_retries_after_terminal_or_missing_runs(tracker):
    tracker.client.delete_merge_request_emoji.side_effect = [OSError("request lost"), None]
    first, second = _run(), _run()
    _start(first)
    _start(second)
    _finish_row(first)
    second.delete()
    ReviewReactions.reconcile()
    group = tracker.group()
    assert not tracker.redis.smembers(group.members)
    assert tracker.redis.smembers(group.groups) == {"42:7"}
    ReviewReactions.reconcile()
    assert tracker.client.delete_merge_request_emoji.call_args_list == [(("42", 7, 101), {})] * 2
    assert not tracker.redis.data


@pytest.mark.django_db
def test_finishing_before_registration_is_not_resurrected(tracker):
    run = _run()

    def finish_first(*_args):
        _finish_row(run)
        ReviewReactions.finish(str(run.pk))
        return 42

    tracker.client.get_repository_id.side_effect = finish_first
    _start(run)
    ReviewReactions.reconcile()
    tracker.client.create_merge_request_emoji.assert_not_called()
    assert not tracker.redis.data


@pytest.mark.django_db
def test_runs_register_even_when_another_worker_holds_the_lease(tracker):
    group = tracker.group()
    lock = tracker.redis.lock(group.lock_name, timeout=15, blocking=False)
    assert lock.acquire()
    first, second = _run(), _run()
    _start(first)
    _start(second)
    _finish_row(first)
    ReviewReactions.finish(str(first.pk))
    assert tracker.redis.smembers(group.members) == {str(second.pk)}
    tracker.client.create_merge_request_emoji.assert_not_called()
    lock.release()
    ReviewReactions.reconcile()
    tracker.client.create_merge_request_emoji.assert_called_once_with("42", 7, Emoji.EYES)


@pytest.mark.django_db
def test_expired_delete_lease_cannot_remove_the_successor_award(tracker):
    first, second = _run(), _run()
    _start(first)
    group = tracker.group()
    tracker.client.create_merge_request_emoji.return_value = 102
    platform_awards = {101}

    def create(*_args):
        assert not platform_awards
        platform_awards.add(102)
        return 102

    tracker.client.create_merge_request_emoji.side_effect = create

    def delete(_project_id, _iid, award_id):
        if tracker.client.delete_merge_request_emoji.call_count == 1:
            # The old delete is still in flight when the successor takes over its intent.
            tracker.redis.locks[group.lock_name].release()
            _start(second)
        platform_awards.discard(award_id)

    tracker.client.delete_merge_request_emoji.side_effect = delete
    _finish_row(first)
    ReviewReactions.finish(str(first.pk))
    assert platform_awards == {102}
    assert tracker.redis.get(group.award) == "102"
    assert tracker.client.delete_merge_request_emoji.call_args_list == [(("42", 7, 101), {})] * 2
    _finish_row(second)
    ReviewReactions.finish(str(second.pk))
    assert not platform_awards
    assert not tracker.redis.data


@pytest.mark.django_db
def test_instance_change_does_not_touch_the_old_hosts_numeric_award(tracker, monkeypatch):
    run = _run()
    _start(run)
    _finish_row(run)
    monkeypatch.setattr(reactions.codebase_settings, "GITLAB_URL", HttpUrl("https://different.example"))
    ReviewReactions.finish(str(run.pk))
    ReviewReactions.reconcile()
    tracker.client.delete_merge_request_emoji.assert_not_called()
    monkeypatch.setattr(reactions.codebase_settings, "GITLAB_URL", HttpUrl("https://gitlab.example"))
    ReviewReactions.reconcile()
    tracker.client.delete_merge_request_emoji.assert_called_once_with("42", 7, 101)


@pytest.mark.django_db
def test_finish_during_post_removes_confirmed_award(tracker):
    run = _run()

    def finish_during_post(*_args):
        _finish_row(run)
        ReviewReactions.finish(str(run.pk))
        return 101

    tracker.client.create_merge_request_emoji.side_effect = finish_during_post
    _start(run)
    tracker.client.delete_merge_request_emoji.assert_called_once_with("42", 7, 101)
    assert not tracker.redis.data


@pytest.mark.django_db
def test_confirmed_duplicate_post_leaves_no_ownership_intent(tracker, caplog):
    tracker.client.create_merge_request_emoji.return_value = None
    run = _run()
    _start(run)
    _finish_row(run)
    ReviewReactions.finish(str(run.pk))
    tracker.client.delete_merge_request_emoji.assert_not_called()
    assert not tracker.redis.data
    assert "Discarding unconfirmed running review reaction" not in caplog.text


@pytest.mark.django_db
def test_a_run_joining_during_delete_gets_a_new_award(tracker):
    first, second = _run(), _run()
    _start(first)
    tracker.client.create_merge_request_emoji.return_value = 102
    tracker.client.delete_merge_request_emoji.side_effect = lambda *_: _start(second)
    _finish_row(first)
    ReviewReactions.finish(str(first.pk))
    group = tracker.group()
    assert tracker.redis.smembers(group.members) == {str(second.pk)}
    assert tracker.redis.get(group.award) == "102"
    tracker.client.delete_merge_request_emoji.assert_called_once_with("42", 7, 101)

    tracker.client.delete_merge_request_emoji.side_effect = None
    _finish_row(second)
    ReviewReactions.finish(str(second.pk))
    tracker.client.delete_merge_request_emoji.assert_called_with("42", 7, 102)
    assert not tracker.redis.data


@pytest.mark.django_db
def test_redis_failure_is_optional_on_every_entrypoint(tracker, caplog):
    tracker.connections.sync_client.side_effect = OSError("Redis unavailable")
    run = _run()
    _start(run)
    ReviewReactions.finish(str(run.pk))
    ReviewReactions.reconcile()
    tracker.client.create_merge_request_emoji.assert_not_called()
    tracker.client.delete_merge_request_emoji.assert_not_called()
    assert "Could not set the running review reaction" in caplog.text
    assert "Could not clear the running review reaction" in caplog.text
    assert "Could not scan the running review reactions" in caplog.text
