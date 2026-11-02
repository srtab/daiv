import uuid
from datetime import timedelta

from django.db.models import OuterRef
from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.models import EventKind, InboxEvent, MergeRequestOwnership, QuietReason, Session, SessionOrigin
from sessions.pipeline_watch.store import WatchStore, gave_up

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

        assert await WatchStore().arecord_pipeline(ownership.pk, 100, "failed") is True
        assert await WatchStore().arecord_pipeline(ownership.pk, 100, "failed") is False
        assert await WatchStore().arecord_pipeline(ownership.pk, 101, "failed") is True
        assert (await MergeRequestOwnership.objects.aget()).last_pipeline_id == 101

    async def test_a_retried_pipeline_with_a_new_status_is_recorded_again(self):
        ownership = await _owned()

        assert await WatchStore().arecord_pipeline(ownership.pk, 100, "failed") is True
        assert await WatchStore().arecord_pipeline(ownership.pk, 100, "success") is True
        stored = await MergeRequestOwnership.objects.aget()
        assert (stored.last_pipeline_id, stored.last_pipeline_status) == (100, "success")


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


class TestGaveUp:
    """A give-up stands until CI of the same subject passes: on GitHub another workflow's pass does not clear it."""

    @staticmethod
    async def _gave_up(session: Session) -> bool:
        since = timezone.now() - timedelta(days=1)
        return await Session.objects.filter(pk=session.pk).filter(gave_up(OuterRef("pk"), since=since)).aexists()

    async def _given_up(self, subject: str) -> Session:
        ownership = await _owned()
        session = await Session.objects.aget(pk=ownership.session_id)
        await amake_event(session, merge_request_iid=7, subject=subject, wake=False, reason=QuietReason.BUDGET_SPENT)
        return session

    async def test_a_pass_of_the_same_subject_clears_it(self):
        session = await self._given_up("ci:7:11")
        assert await self._gave_up(session) is True

        await amake_event(session, EventKind.CI_PASSED, merge_request_iid=7, subject="ci:7:11")

        assert await self._gave_up(session) is False

    async def test_another_workflows_pass_does_not(self):
        session = await self._given_up("ci:7:11")

        await amake_event(session, EventKind.CI_PASSED, merge_request_iid=7, subject="ci:7:12")

        assert await self._gave_up(session) is True


class TestQuiet:
    """What the reconciler re-reads: an open, watched merge request nothing was heard of for a while, whatever CI it
    last reported, since a missed webhook (a manual retry's, say) can follow any result."""

    @staticmethod
    async def _quiet(limit: int = 10) -> list[int]:
        rows = await sync_to_async(list)(WatchStore().quiet(since=timezone.now() - timedelta(minutes=30), limit=limit))
        return [row["merge_request_iid"] for row in rows]

    @staticmethod
    async def _result(ownership: MergeRequestOwnership, *, age: timedelta, **fields) -> None:
        session = await Session.objects.aget(pk=ownership.session_id)
        event = await amake_event(session, EventKind.CI_PASSED, merge_request_iid=ownership.merge_request_iid, **fields)
        await InboxEvent.objects.filter(pk=event.pk).aupdate(created_at=timezone.now() - age)

    async def test_a_merge_request_with_no_ci_result_since_it_was_claimed_is_quiet(self):
        await _owned(7, age=timedelta(hours=1))

        assert await self._quiet() == [7]

    async def test_one_whose_last_result_is_old_is_quiet_even_when_it_judged_daivs_commit(self):
        ownership = await _owned(7, last_daiv_sha="abc", age=timedelta(hours=2))
        await self._result(ownership, age=timedelta(hours=1), payload=ci_payload(sha="abc", status="success"))

        assert await self._quiet() == [7]

    @pytest.mark.parametrize(
        "fields", [{}, {"wake": False, "reason": QuietReason.WATCH_OFF}], ids=["result", "kept-quiet"]
    )
    async def test_a_recent_ci_result_is_not_quiet(self, fields):
        ownership = await _owned(7, age=timedelta(hours=1))
        await self._result(ownership, age=timedelta(minutes=5), **fields)

        assert await self._quiet() == []

    async def test_the_stalest_come_first(self):
        heard_long_ago = await _owned(7, branch="a", age=timedelta(hours=5))
        await self._result(heard_long_ago, age=timedelta(hours=3))
        await _owned(8, branch="b", age=timedelta(days=3))
        await _owned(9, branch="c", age=timedelta(hours=1))

        assert await self._quiet() == [8, 7, 9]
        assert await self._quiet(limit=2) == [8, 7]

    async def test_a_recent_re_read_counts_as_hearing_of_it(self):
        ownership = await _owned(7, age=timedelta(hours=1))
        await _owned(8, branch="b", age=timedelta(hours=2))

        await WatchStore().amark_reconciled(ownership.pk, at=timezone.now() - timedelta(minutes=5))

        assert await self._quiet() == [8]
        await ownership.arefresh_from_db()
        assert ownership.updated_at < timezone.now() - timedelta(minutes=30)

    @pytest.mark.parametrize(
        ("age", "fields"),
        [
            (timedelta(minutes=5), {}),
            (timedelta(hours=1), {"watch_enabled": False}),
            (timedelta(hours=1), {"closed_at": timezone.now()}),
        ],
        ids=["claimed-recently", "watch-off", "closed"],
    )
    async def test_the_rest_is_left_alone(self, age, fields):
        await _owned(7, age=age, **fields)

        assert await self._quiet() == []


class TestJudged:
    @staticmethod
    async def _judged(*, watch_on: bool) -> bool:
        ownership = await MergeRequestOwnership.objects.aget()
        return await WatchStore().ajudged(ownership.session_id, "ci:7", 100, "failed", watch_on=watch_on)

    @staticmethod
    async def _failure(**fields) -> None:
        ownership = await _owned(7)
        session = await Session.objects.aget(pk=ownership.session_id)
        await amake_event(
            session, EventKind.CI_FAILED, merge_request_iid=7, subject="ci:7", payload=ci_payload(100, status="failed"),
            **fields,
        )  # fmt: skip

    async def test_a_result_posted_in_its_status_was_judged(self):
        await self._failure(wake=False, reason=QuietReason.ALREADY_EXAMINED)

        assert await self._judged(watch_on=True) is True

    async def test_a_result_kept_quiet_by_the_watch_being_off_counts_only_while_it_is_off(self):
        await self._failure(wake=False, reason=QuietReason.WATCH_OFF)

        assert (await self._judged(watch_on=False), await self._judged(watch_on=True)) == (True, False)


class TestPassed:
    @staticmethod
    async def _post(session: Session, kind: str, status: str) -> None:
        await amake_event(session, kind, merge_request_iid=7, subject="ci:7:11", payload=ci_payload(100, status=status))

    async def test_only_a_latest_result_that_is_the_pipelines_pass_counts(self):
        ownership = await _owned(7)
        session = await Session.objects.aget(pk=ownership.session_id)
        store = WatchStore()

        assert await store.apassed(session.pk, "ci:7:11", 100, "success") is False
        await self._post(session, EventKind.CI_PASSED, "success")
        assert await store.apassed(session.pk, "ci:7:11", 100, "success") is True
        await self._post(session, EventKind.CI_FAILED, "failed")
        assert await store.apassed(session.pk, "ci:7:11", 100, "failed") is False
