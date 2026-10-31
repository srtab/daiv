import uuid
from datetime import timedelta

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.models import EventKind, MergeRequestOwnership, QuietReason, Session, SessionOrigin
from sessions.pipeline_watch.store import WatchStore

from tests.unit_tests.sessions.inbox.conftest import amake_event, ci_payload

pytestmark = pytest.mark.django_db(transaction=True)

REPO = "group/repo"


async def _owned(iid: int = 7, *, branch: str = "daiv/branch", age: timedelta | None = None, **fields):
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id=REPO, ref=branch
    )
    ownership = await MergeRequestOwnership.objects.acreate(
        repo_id=REPO, merge_request_iid=iid, session=session, source_branch=branch, **fields
    )
    if age is not None:
        await MergeRequestOwnership.objects.filter(pk=ownership.pk).aupdate(updated_at=timezone.now() - age)
    return ownership


class TestOpen:
    async def test_it_finds_the_open_ownership_of_a_branch(self):
        ownership = await _owned()
        await _owned(8, branch="closed", closed_at=timezone.now())

        assert (await WatchStore().aopen(REPO, "daiv/branch")).pk == ownership.pk
        assert await WatchStore().ahas_open(REPO, "daiv/branch") is True
        assert await WatchStore().aopen(REPO, "closed") is None
        assert await WatchStore().ahas_open(REPO, "closed") is False


class TestRecordPipeline:
    async def test_only_the_first_caller_records_a_pipeline(self):
        ownership = await _owned()

        assert await WatchStore().arecord_pipeline(ownership.pk, 100) is True
        assert await WatchStore().arecord_pipeline(ownership.pk, 100) is False
        assert await WatchStore().arecord_pipeline(ownership.pk, 101) is True
        assert (await MergeRequestOwnership.objects.aget()).last_pipeline_id == 101


class TestSetEnabled:
    async def test_turning_it_on_gives_the_attempts_back(self):
        ownership = await _owned(watch_enabled=False, attempts=3)

        await WatchStore().aset_enabled(ownership.pk, enabled=True)

        await ownership.arefresh_from_db()
        assert (ownership.watch_enabled, ownership.attempts) == (True, 0)

    async def test_turning_it_off_keeps_them(self):
        ownership = await _owned(attempts=2)

        await WatchStore().aset_enabled(ownership.pk, enabled=False)

        await ownership.arefresh_from_db()
        assert (ownership.watch_enabled, ownership.attempts) == (False, 2)


class TestUnjudged:
    async def _unjudged(self) -> list[int]:
        now = timezone.now()
        rows = await sync_to_async(list)(
            WatchStore().unjudged(quiet_since=now - timedelta(minutes=30), horizon=now - timedelta(hours=6), limit=10)
        )
        return sorted(row.merge_request_iid for row in rows)

    async def test_a_quiet_daiv_commit_without_a_ci_result_is_unjudged(self):
        await _owned(7, last_daiv_sha="abc", age=timedelta(hours=1))

        assert await self._unjudged() == [7]

    async def test_a_daiv_commit_with_a_ci_result_is_judged(self):
        ownership = await _owned(7, last_daiv_sha="abc", age=timedelta(hours=1))
        session = await Session.objects.aget(pk=ownership.session_id)
        await amake_event(session, EventKind.CI_PASSED, merge_request_iid=7, payload=ci_payload(sha="abc"))

        assert await self._unjudged() == []

    async def test_a_result_recorded_while_the_watch_was_off_does_not_count_once_it_is_on(self):
        ownership = await _owned(7, last_daiv_sha="abc", age=timedelta(hours=1))
        session = await Session.objects.aget(pk=ownership.session_id)
        await amake_event(
            session, merge_request_iid=7, payload=ci_payload(sha="abc"), wake=False, reason=QuietReason.WATCH_OFF
        )

        assert await self._unjudged() == [7]

    async def test_a_ci_result_for_an_older_commit_does_not_count(self):
        ownership = await _owned(7, last_daiv_sha="abc", age=timedelta(hours=1))
        session = await Session.objects.aget(pk=ownership.session_id)
        await amake_event(session, EventKind.CI_FAILED, merge_request_iid=7, payload=ci_payload(sha="older"))

        assert await self._unjudged() == [7]

    @pytest.mark.parametrize(
        ("age", "fields"),
        [
            (timedelta(minutes=5), {}),
            (timedelta(hours=7), {}),
            (timedelta(hours=1), {"watch_enabled": False}),
            (timedelta(hours=1), {"closed_at": timezone.now()}),
        ],
        ids=["too-recent", "past-the-horizon", "watch-off", "closed"],
    )
    async def test_the_rest_is_left_alone(self, age, fields):
        await _owned(7, last_daiv_sha="abc", age=age, **fields)

        assert await self._unjudged() == []
