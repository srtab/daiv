from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.inbox.drain import Drain
from sessions.inbox.merge_request_jobs import (
    ARCHIVED,
    NOT_STARTED_CLOSED,
    NOT_STARTED_MERGED,
    MergeRequestJobRefused,
    MergeRequestJobs,
)
from sessions.inbox.next_runs import NextRuns
from sessions.inbox.post import Inbox
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.queue import dispatch_next_queued

from tests.unit_tests.sessions.conftest import amake_owned_merge_request
from tests.unit_tests.sessions.inbox.conftest import mention_payload, post_merge_request_job

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def drain():
    with patch.object(Drain, "start_logged", autospec=True) as drain:
        yield drain


@pytest.fixture
async def owned():
    return await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=7)


async def _post(owned, **fields) -> Run:
    return await sync_to_async(post_merge_request_job)(owned, **fields)


async def _busy(owned) -> None:
    await Session.objects.filter(pk=owned.session_id).aupdate(active_run_id="another", last_active_at=timezone.now())


class TestPost:
    async def test_it_creates_the_event_and_its_queued_run_linked(self, owned, drain):
        run = await _post(owned, muted=True, source=SessionOrigin.MCP_JOB)

        stored = await Run.objects.aget(pk=run.pk)
        assert (stored.status, stored.trigger_type, stored.runs_alone, stored.acts_as_user) == (
            RunStatus.QUEUED, SessionOrigin.MCP_JOB, True, False,
        )  # fmt: skip
        assert (stored.user_id, stored.external_provider, stored.external_uid, stored.external_username) == (
            None, "gitlab", "9", "pusher",
        )  # fmt: skip
        assert (stored.muted, stored.prompt, stored.merge_request_iid, stored.ref) == (
            True, "/code-review", 7, owned.source_branch,
        )  # fmt: skip
        event = await InboxEvent.objects.aget(run=run)
        assert (event.kind, event.wake, event.subject, event.state, event.merge_request_iid) == (
            EventKind.MERGE_REQUEST_JOB, True, "", EventState.PENDING, 7,
        )  # fmt: skip
        assert event.payload == {
            "prompt": "/code-review", "acts_as_user": False, "source": SessionOrigin.MCP_JOB, "muted": True,
        }  # fmt: skip
        drain.assert_called_once()

    async def test_a_linked_requester_owns_its_run(self, owned, drain, member_user):
        run = await _post(owned, user=member_user, acts_as_user=True, username="", provider="", uid="")

        stored = await Run.objects.aget(pk=run.pk)
        assert (stored.user_id, stored.acts_as_user) == (member_user.pk, True)
        assert (await InboxEvent.objects.aget(run=run)).author_id == member_user.pk

    async def test_an_archived_session_is_refused_and_nothing_is_created(self, owned, drain):
        await Session.objects.filter(pk=owned.session_id).aupdate(archived_at=timezone.now())

        with pytest.raises(MergeRequestJobRefused) as refused:
            await _post(owned)

        assert refused.value.reason == ARCHIVED
        assert not await InboxEvent.objects.aexists()
        assert not await Run.objects.aexists()

    @pytest.mark.parametrize(("merged", "state"), [(True, MergeRequestState.MERGED), (False, MergeRequestState.CLOSED)])
    async def test_a_merge_request_no_longer_open_is_refused(self, owned, drain, merged, state):
        await MergeRequestOwnership.objects.filter(pk=owned.pk).aupdate(merged=merged, closed_at=timezone.now())

        with pytest.raises(MergeRequestJobRefused) as refused:
            await _post(owned)

        assert refused.value.reason == state
        assert not await Run.objects.aexists()

    async def test_a_draft_counts_as_open(self, owned, drain):
        await MergeRequestOwnership.objects.filter(pk=owned.pk).aupdate(draft=True)

        assert (await _post(owned)).status == RunStatus.QUEUED

    async def test_two_jobs_from_one_person_on_one_branch_each_get_their_own_run(self, owned, drain):
        first = await _post(owned, request_key="job:1")
        second = await _post(owned, request_key="job:2")

        assert first.pk != second.pk
        assert await InboxEvent.objects.filter(kind=EventKind.MERGE_REQUEST_JOB, state=EventState.PENDING).acount() == 2

    async def test_its_run_is_no_next_run_and_the_queue_leaves_it_queued(self, owned, drain):
        run = await _post(owned)
        session = await Session.objects.aget(pk=owned.session_id)

        assert not await sync_to_async(lambda: list(NextRuns(session).queued()))()
        with patch("sessions.queue.run_job_task") as task:
            task.aenqueue = AsyncMock()
            await sync_to_async(dispatch_next_queued)(session.pk)

        task.aenqueue.assert_not_awaited()
        assert (await Run.objects.aget(pk=run.pk)).status == RunStatus.QUEUED

    @pytest.mark.parametrize("linked", [True, False], ids=["user", "account"])
    async def test_a_mention_from_the_same_person_gets_an_ordinary_next_run(self, owned, member_user, linked):
        await _busy(owned)
        person = (
            {"user": member_user, "acts_as_user": True, "username": "", "provider": "", "uid": ""} if linked else {}
        )
        with patch.object(Drain, "start_logged"):
            job_run = await _post(owned, **person)
            result = await Inbox(owned.session_id).apost(
                kind=EventKind.MENTION,
                source_key="note:1",
                payload=mention_payload(),
                wake=True,
                ref=owned.source_branch,
                merge_request_iid=7,
                author=member_user if linked else None,
                author_username="pusher",
                author_provider="" if linked else "gitlab",
                author_uid="" if linked else "9",
            )

        assert result.run_id is not None
        assert result.run_id != job_run.pk
        assert await Run.objects.filter(session_id=owned.session_id, status=RunStatus.QUEUED).acount() == 2
        assert (await InboxEvent.objects.aget(run=job_run)).kind == EventKind.MERGE_REQUEST_JOB


class TestStopReason:
    async def test_none_while_the_merge_request_is_open(self, owned, drain):
        await _post(owned)
        event = await InboxEvent.objects.select_related("session").aget(kind=EventKind.MERGE_REQUEST_JOB)

        assert await sync_to_async(MergeRequestJobs.stop_reason)(event.session, event) is None

    @pytest.mark.parametrize(("merged", "failure"), [(True, EventFailure.MR_MERGED), (False, EventFailure.MR_CLOSED)])
    async def test_the_merge_requests_end_or_the_sessions_archive_stops_it(self, owned, drain, merged, failure):
        await _post(owned)
        await MergeRequestOwnership.objects.filter(pk=owned.pk).aupdate(merged=merged, closed_at=timezone.now())
        event = await InboxEvent.objects.select_related("session").aget(kind=EventKind.MERGE_REQUEST_JOB)

        assert await sync_to_async(MergeRequestJobs.stop_reason)(event.session, event) == failure
        event.session.archived_at = timezone.now()
        assert await sync_to_async(MergeRequestJobs.stop_reason)(event.session, event) == EventFailure.SESSION_ARCHIVED


class TestStop:
    @pytest.mark.parametrize(
        ("failure", "note"),
        [(EventFailure.MR_MERGED, NOT_STARTED_MERGED), (EventFailure.MR_CLOSED, NOT_STARTED_CLOSED)],
    )
    async def test_it_fails_the_job_its_run_and_says_why_on_the_merge_request(
        self, owned, drain, mock_repo_client, failure, note
    ):
        run = await _post(owned)
        event = await InboxEvent.objects.aget(run=run)

        await sync_to_async(MergeRequestJobs.stop)(event, failure, run)

        stored = await InboxEvent.objects.aget(pk=event.pk)
        assert (stored.state, stored.failure) == (EventState.FAILED, failure)
        stopped = await Run.objects.aget(pk=run.pk)
        assert (stopped.status, stopped.error_message) == (RunStatus.FAILED, note)
        mock_repo_client.create_merge_request_comment.assert_called_once_with("group/repo", 7, note)

    async def test_an_archived_session_posts_nothing(self, owned, drain, mock_repo_client):
        run = await _post(owned)
        event = await InboxEvent.objects.aget(run=run)

        await sync_to_async(MergeRequestJobs.stop)(event, EventFailure.SESSION_ARCHIVED, run)

        assert (await Run.objects.aget(pk=run.pk)).status == RunStatus.FAILED
        mock_repo_client.create_merge_request_comment.assert_not_called()


class TestEndWithRun:
    async def test_a_cancelled_run_dismisses_its_job_and_owes_no_note(self, owned, drain):
        run = await _post(owned)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, cancel_requested_at=timezone.now())

        assert await sync_to_async(MergeRequestJobs.end_with_run)(await Run.objects.aget(pk=run.pk)) == []
        assert (await InboxEvent.objects.aget(run=run)).state == EventState.DISMISSED

    async def test_a_failed_run_fails_its_job_and_owes_a_note(self, owned, drain):
        run = await _post(owned)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED)

        failed = await sync_to_async(MergeRequestJobs.end_with_run)(await Run.objects.aget(pk=run.pk))

        stored = await InboxEvent.objects.aget(run=run)
        assert [event.pk for event in failed] == [stored.pk]
        assert (stored.state, stored.failure) == (EventState.FAILED, EventFailure.RUN_FAILED)

    async def test_a_successful_run_leaves_its_job_to_the_shared_path(self, owned, drain):
        run = await _post(owned)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.SUCCESSFUL)

        assert await sync_to_async(MergeRequestJobs.end_with_run)(await Run.objects.aget(pk=run.pk)) == []
        assert (await InboxEvent.objects.aget(run=run)).state == EventState.PENDING
