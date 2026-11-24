from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from mcp_connectors.models import MCPServer
from sessions.inbox.drain import Drain
from sessions.inbox.merge_request_jobs import (
    ARCHIVED,
    INTERRUPTED_NOTE,
    NOT_STARTED_ACCESS_DENIED,
    NOT_STARTED_BRANCH_GONE,
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

from core.constants import INTERRUPTED_MESSAGE
from core.models import ProviderFailureKind
from tests.unit_tests.mcp_connectors.helpers import only_servers
from tests.unit_tests.sessions.conftest import amake_owned_merge_request
from tests.unit_tests.sessions.inbox.conftest import mention_payload, post_merge_request_job
from tests.unit_tests.webhooks.conftest import make_db_task_result

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
async def owned():
    return await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=7)


async def _post(owned, **fields) -> Run:
    return await sync_to_async(post_merge_request_job)(owned, **fields)


async def _busy(owned) -> None:
    await Session.objects.filter(pk=owned.session_id).aupdate(active_run_id="another", last_active_at=timezone.now())


class TestPost:
    async def test_it_creates_the_event_and_its_queued_run_linked(self, owned, drain):
        run = await _post(owned, muted=True, trigger_type=SessionOrigin.MCP_JOB)

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
        assert event.payload == {"prompt": "/code-review", "trigger_type": SessionOrigin.MCP_JOB, "muted": True}
        drain.assert_called_once()

    async def test_it_carries_the_jobs_references_mcp_switches_and_named_environment(self, owned, drain):
        run = await _post(
            owned, references=[{"key": "PROJ-1"}], mcp_switches={"grafana": True}, sandbox_environment_id="env-1"
        )

        payload = (await InboxEvent.objects.aget(run=run)).payload
        assert (payload["references"], payload["mcp_switches"], payload["sandbox_environment_id"]) == (
            [{"key": "PROJ-1"}], {"grafana": True}, "env-1",
        )  # fmt: skip

    async def test_a_merge_request_of_another_of_the_sessions_repositories_runs_on_the_sessions_branch(
        self, owned, drain
    ):
        other = await MergeRequestOwnership.objects.acreate(
            repo_id="group/other", merge_request_iid=3, session_id=owned.session_id, source_branch="daiv/other"
        )

        run = await _post(other)

        stored = await Run.objects.aget(pk=run.pk)
        assert (stored.repo_id, stored.ref, stored.merge_request_iid) == ("group/repo", owned.source_branch, None)
        event = await InboxEvent.objects.aget(run=run)
        assert (event.repo_id, event.ref, event.merge_request_iid) == ("group/other", "daiv/other", 3)

    async def test_a_linked_caller_owns_its_run(self, owned, drain, member_user):
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
        [
            (EventFailure.MR_MERGED, NOT_STARTED_MERGED),
            (EventFailure.MR_CLOSED, NOT_STARTED_CLOSED),
            (EventFailure.ACCESS_DENIED, NOT_STARTED_ACCESS_DENIED),
        ],
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
    async def test_a_cancelled_run_dismisses_its_job_and_posts_nothing(self, owned, drain, mock_repo_client):
        run = await _post(owned)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, cancel_requested_at=timezone.now())

        await sync_to_async(MergeRequestJobs.end_with_run)(await Run.objects.aget(pk=run.pk))

        assert (await InboxEvent.objects.aget(run=run)).state == EventState.DISMISSED
        mock_repo_client.create_merge_request_comment.assert_not_called()

    async def test_a_failed_run_fails_its_job_and_says_so_on_the_merge_request_once(
        self, owned, drain, mock_repo_client
    ):
        run = await _post(owned)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED)
        failed = await Run.objects.aget(pk=run.pk)

        await sync_to_async(MergeRequestJobs.end_with_run)(failed)
        await sync_to_async(MergeRequestJobs.end_with_run)(failed)

        stored = await InboxEvent.objects.aget(run=run)
        assert (stored.state, stored.failure) == (EventState.FAILED, EventFailure.RUN_FAILED)
        mock_repo_client.create_merge_request_comment.assert_called_once()
        repo_id, iid, body = mock_repo_client.create_merge_request_comment.call_args.args
        assert (repo_id, iid) == ("group/repo", 7)
        assert body.startswith("An unexpected error occurred")

    @pytest.mark.parametrize(
        ("fields", "note"),
        [
            ({"error_message": INTERRUPTED_MESSAGE, "task_result_id": None}, INTERRUPTED_NOTE),
            ({"failure_kind": ProviderFailureKind.RATE_LIMITED, "failure_provider": "anthropic"}, "rate"),
        ],
        ids=["interrupted", "provider"],
    )
    async def test_the_note_says_what_stopped_the_run(self, owned, drain, mock_repo_client, fields, note):
        run = await _post(owned)
        if "task_result_id" in fields:
            fields["task_result_id"] = await make_db_task_result()
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, **fields)

        await sync_to_async(MergeRequestJobs.end_with_run)(await Run.objects.aget(pk=run.pk))

        body = mock_repo_client.create_merge_request_comment.call_args.args[2]
        assert note.lower() in body.lower()
        assert not body.startswith("An unexpected error occurred")

    async def test_a_successful_run_leaves_its_job_to_the_shared_path(self, owned, drain, mock_repo_client):
        run = await _post(owned)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.SUCCESSFUL)

        await sync_to_async(MergeRequestJobs.end_with_run)(await Run.objects.aget(pk=run.pk))

        assert (await InboxEvent.objects.aget(run=run)).state == EventState.PENDING
        mock_repo_client.create_merge_request_comment.assert_not_called()


class TestSettleUnstartable:
    async def test_a_job_whose_run_failed_before_the_drain_took_it_says_so_on_the_merge_request(
        self, owned, drain, mock_repo_client
    ):
        run = await _post(owned)
        event = await InboxEvent.objects.aget(run=run)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED)

        await sync_to_async(MergeRequestJobs.settle_unstartable)(event, await Run.objects.aget(pk=run.pk))

        assert (await InboxEvent.objects.aget(pk=event.pk)).state == EventState.FAILED
        mock_repo_client.create_merge_request_comment.assert_called_once()


class TestCallerFailureMessage:
    async def test_a_job_that_never_started_tells_its_caller_why(self, owned, drain):
        run = await _post(owned)
        event = await InboxEvent.objects.aget(run=run)
        await sync_to_async(MergeRequestJobs.stop)(event, EventFailure.ACCESS_DENIED, run)

        assert MergeRequestJobs.caller_failure_message(await Run.objects.aget(pk=run.pk)) == NOT_STARTED_ACCESS_DENIED

    async def test_a_job_whose_source_branch_was_gone_tells_its_caller_why(self, owned, drain):
        run = await _post(owned)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, error_message=NOT_STARTED_BRANCH_GONE)

        assert MergeRequestJobs.caller_failure_message(await Run.objects.aget(pk=run.pk)) == NOT_STARTED_BRANCH_GONE

    async def test_any_other_failure_is_kept_from_its_caller(self, owned, drain):
        run = await _post(owned)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, error_message="KeyError: 'token'")

        assert MergeRequestJobs.caller_failure_message(await Run.objects.aget(pk=run.pk)) is None


class TestRunOptions:
    async def _options(self, owned, *, acting_user_id=None):
        session = await Session.objects.aget(pk=owned.session_id)
        events = [event async for event in InboxEvent.objects.filter(session=session)]
        return await sync_to_async(MergeRequestJobs.run_options)(events, session, acting_user_id=acting_user_id)

    async def test_without_a_job_a_run_uses_the_sessions_own(self, owned):
        await Session.objects.filter(pk=owned.session_id).aupdate(
            mcp_overrides={"rt": "off"}, external_refs=[{"key": "PROJ-0"}]
        )

        options = await self._options(owned)

        assert options.mcp_overrides == {"rt": "off"}
        assert [ref.key for ref in options.references] == ["PROJ-0"]

    async def test_a_jobs_switches_and_references_apply_on_top_of_the_sessions_for_its_run(self, owned, drain):
        await sync_to_async(only_servers)(("rt", MCPServer.Status.ACTIVE), ("grafana", MCPServer.Status.ON_DEMAND))
        await Session.objects.filter(pk=owned.session_id).aupdate(
            mcp_overrides={"rt": "off"}, external_refs=[{"key": "PROJ-0"}]
        )
        await _post(owned, references=[{"key": "PROJ-1"}], mcp_switches={"grafana": True, "gone": True})

        options = await self._options(owned)

        assert options.mcp_overrides == {"rt": "off", "grafana": "on"}
        assert [ref.key for ref in options.references] == ["PROJ-0", "PROJ-1"]
        session = await Session.objects.aget(pk=owned.session_id)
        assert (session.mcp_overrides, session.external_refs) == ({"rt": "off"}, [{"key": "PROJ-0"}])
