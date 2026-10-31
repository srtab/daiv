from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.executor.lock import stale_cutoff
from sessions.inbox.complete import MAX_EVENT_ATTEMPTS, aon_run_ended, recover_stale_events
from sessions.models import EventFailure, EventKind, EventState, InboxEvent, Run, RunStatus, Session, SessionOrigin

from codebase.clients.base import CommentThread
from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session, mention_payload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def drain():
    with patch("sessions.inbox.complete.drain_logged") as drain:
        yield drain


async def _run_with_event(status: str, *, attempts: int = 0, **run_fields) -> tuple[Run, InboxEvent]:
    session = await amake_session()
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id, status=status,
        **run_fields,
    )  # fmt: skip
    event = await amake_event(session, EventKind.CI_FAILED, run=run, state=EventState.TAKEN, attempts=attempts)
    return run, event


async def _state(event: InboxEvent) -> InboxEvent:
    return await InboxEvent.objects.aget(pk=event.pk)


class TestOutcomes:
    @pytest.mark.parametrize("status", [RunStatus.SUCCESSFUL, RunStatus.WAITING_INPUT])
    async def test_a_run_that_did_its_job_handles_its_events(self, drain, status):
        run, event = await _run_with_event(status)

        await aon_run_ended(run)

        assert (await _state(event)).state == EventState.HANDLED
        drain.assert_called_once_with(run.session_id)

    async def test_a_run_its_owner_stopped_dismisses_its_events(self, drain):
        run, event = await _run_with_event(RunStatus.FAILED, cancel_requested_at=timezone.now())

        await aon_run_ended(run)

        assert (await _state(event)).state == EventState.DISMISSED
        drain.assert_called_once_with(run.session_id)

    async def test_a_failed_run_puts_its_events_back_for_another_attempt(self, drain):
        run, event = await _run_with_event(RunStatus.FAILED)

        await aon_run_ended(run)

        stored = await _state(event)
        assert (stored.state, stored.attempts, stored.run_id) == (EventState.PENDING, 1, None)
        drain.assert_called_once_with(run.session_id)

    async def test_the_last_attempt_fails_the_events_and_logs(self, drain, caplog):
        run, event = await _run_with_event(RunStatus.FAILED, attempts=MAX_EVENT_ATTEMPTS - 1)

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            await aon_run_ended(run)

        stored = await _state(event)
        assert (stored.state, stored.failure) == (EventState.FAILED, EventFailure.RETRIES_EXHAUSTED)
        assert stored.attempts == MAX_EVENT_ATTEMPTS
        assert "failed after 3 attempts" in caplog.text
        drain.assert_called_once_with(run.session_id)

    async def test_events_the_run_did_not_take_are_left_alone(self, drain):
        run, _ = await _run_with_event(RunStatus.FAILED)
        pending = await amake_event(await Session.objects.aget(pk=run.session_id), EventKind.NOTICE, run=run)

        await aon_run_ended(run)

        assert (await _state(pending)).attempts == 0

    async def test_a_settling_failure_is_logged_and_still_drains(self, drain, caplog):
        run, _ = await _run_with_event(RunStatus.SUCCESSFUL)

        with (
            patch("sessions.inbox.complete.settle_events", side_effect=RuntimeError("db down")),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            await aon_run_ended(run)

        assert "settling the events" in caplog.text
        drain.assert_called_once_with(run.session_id)


class TestRecoverStaleEvents:
    async def test_events_of_a_run_that_died_with_its_slot_are_retried(self):
        run, event = await _run_with_event(RunStatus.RUNNING)
        await Session.objects.filter(pk=run.session_id).aupdate(
            active_run_id=str(run.pk), last_active_at=stale_cutoff() - timedelta(minutes=1)
        )

        assert await _recover() == 1

        stored = await _state(event)
        assert (stored.state, stored.attempts) == (EventState.PENDING, 1)

    async def test_events_of_a_run_that_ended_unsettled_follow_its_outcome(self):
        run, event = await _run_with_event(RunStatus.SUCCESSFUL)
        await Session.objects.filter(pk=run.session_id).aupdate(last_active_at=stale_cutoff() - timedelta(minutes=1))

        assert await _recover() == 1

        assert (await _state(event)).state == EventState.HANDLED

    async def test_events_on_a_live_slot_are_left_alone(self):
        run, event = await _run_with_event(RunStatus.RUNNING)

        assert await _recover() == 0

        assert (await _state(event)).state == EventState.TAKEN


async def _recover() -> int:
    return await sync_to_async(recover_stale_events)()


class TestOwnerlessCiReport:
    async def _ci_run(self, *, user=None, status=RunStatus.SUCCESSFUL, wake=True) -> Run:
        session = await amake_session(user=user)
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id, status=status,
            result_summary="The failure is a GitHub auth error, not the change; I pushed nothing.",
        )  # fmt: skip
        await amake_event(session, EventKind.CI_FAILED, run=run, state=EventState.TAKEN, merge_request_iid=9, wake=wake)
        return run

    async def test_a_session_without_a_user_gets_the_summary_on_the_merge_request(self, drain):
        run = await self._ci_run()

        with patch("sessions.pipeline_watch.platform.WatchPlatform.apost_note") as note:
            await aon_run_ended(run)

        note.assert_awaited_once_with(merge_request_iid=9, body=run.result_summary)

    @pytest.mark.parametrize(
        ("owned", "status", "wake"),
        [
            (True, RunStatus.SUCCESSFUL, True),
            (False, RunStatus.WAITING_INPUT, True),
            (False, RunStatus.SUCCESSFUL, False),
        ],
        ids=["owned", "asked-a-question", "not-woken-by-ci"],
    )
    async def test_otherwise_nothing_is_posted(self, drain, member_user, owned, status, wake):
        run = await self._ci_run(user=member_user if owned else None, status=status, wake=wake)

        with patch("sessions.pipeline_watch.platform.WatchPlatform.apost_note") as note:
            await aon_run_ended(run)

        note.assert_not_called()

    async def test_a_failed_post_is_logged_and_changes_no_event(self, drain, caplog):
        run = await self._ci_run()

        with (
            patch("codebase.clients.RepoClient.create_instance", side_effect=RuntimeError("no client")),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            await aon_run_ended(run)

        assert "failed to comment on" in caplog.text
        assert caplog.records[-1].exc_info is not None
        assert await InboxEvent.objects.filter(run=run, state=EventState.HANDLED).acount() == 1


class TestDelegatedNoPush:
    async def _ci_run(self, *, pushed_sha: str = "", parent: bool = True, status=RunStatus.SUCCESSFUL) -> Run:
        coordinator = await amake_session(repo_id="group/coordinator") if parent else None
        session = await amake_session(parent=coordinator)
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id, status=status,
            pushed_sha=pushed_sha,
        )  # fmt: skip
        await amake_event(session, EventKind.CI_FAILED, run=run, state=EventState.TAKEN, merge_request_iid=9)
        return run

    async def test_a_ci_run_that_pushed_nothing_is_remembered_by_the_parent(self, drain):
        run = await self._ci_run()
        session = await Session.objects.aget(pk=run.session_id)

        with patch("sessions.inbox.post.drain_logged"):
            await aon_run_ended(run)

        event = await InboxEvent.objects.aget(session_id=session.parent_id)
        assert (event.kind, event.wake, event.source_key) == (
            EventKind.DELEGATED_CI_OUTCOME, False, f"child-ci:{session.thread_id}:830724",
        )  # fmt: skip
        assert event.payload == {
            "child_thread_id": session.thread_id,
            "merge_request_iid": 9,
            "outcome": "no_push",
            "web_url": "https://gitlab.example/pipelines/830724",
        }

    @pytest.mark.parametrize(
        ("fields"),
        [{"pushed_sha": "f00dfeed"}, {"parent": False}, {"status": RunStatus.FAILED}],
        ids=["pushed", "no-parent", "failed"],
    )
    async def test_otherwise_no_parent_hears_of_it(self, drain, fields):
        run = await self._ci_run(**fields)

        with patch("sessions.inbox.post.drain_logged"):
            await aon_run_ended(run)

        assert not await InboxEvent.objects.filter(kind=EventKind.DELEGATED_CI_OUTCOME).aexists()


class TestMentionReplies:
    @pytest.fixture
    def client(self):
        client = MagicMock()
        with patch("codebase.clients.RepoClient.create_instance", return_value=client):
            yield client

    async def _run(self, status=RunStatus.SUCCESSFUL, summary="Renamed it in the three call sites.") -> Run:
        session = await amake_session()
        return await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.MR_WEBHOOK, repo_id=session.repo_id, status=status,
            result_summary=summary,
        )  # fmt: skip

    async def _mention(self, run, note_id, *, iid=9, discussion="d", is_diff=False):
        payload = mention_payload(note_id=note_id) | {"discussion_id": f"{discussion}{note_id}", "is_diff": is_diff}
        return await amake_event(
            await Session.objects.aget(pk=run.session_id), EventKind.MENTION, run=run, state=EventState.TAKEN,
            merge_request_iid=iid, payload=payload,
        )  # fmt: skip

    @pytest.mark.parametrize("status", [RunStatus.SUCCESSFUL, RunStatus.WAITING_INPUT])
    async def test_one_mention_gets_the_final_message_in_its_thread(self, drain, client, status):
        run = await self._run(status=status)
        await self._mention(run, 5521, is_diff=True)

        await aon_run_ended(run)

        client.create_merge_request_comment.assert_called_once_with(
            "group/repo", 9, "Renamed it in the three call sites.", reply_to=CommentThread("d5521", on_diff=True)
        )

    async def test_several_mentions_get_the_reply_in_the_latest_thread_and_a_pointer_in_the_others(self, drain, client):
        run = await self._run()
        await self._mention(run, 1)
        await self._mention(run, 2)
        await self._mention(run, 3, iid=10)

        await aon_run_ended(run)

        calls = [
            (c.args[1], c.args[2], c.kwargs["reply_to"].id) for c in client.create_merge_request_comment.call_args_list
        ]
        assert calls[0] == (9, "Renamed it in the three call sites.", "d2")
        assert calls[1][::2] == (9, "d1") and "https://gitlab.example/mr/9#2" in calls[1][1]
        assert calls[2] == (10, "Renamed it in the three call sites.", "d3")
        assert len(calls) == 3

    async def test_a_failed_post_is_logged_and_the_events_stay_handled(self, drain, client, caplog):
        run = await self._run()
        event = await self._mention(run, 5521)
        client.create_merge_request_comment.side_effect = RuntimeError("platform down")

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            await aon_run_ended(run)

        assert "could not reply to the mentions" in caplog.text
        assert caplog.records[-1].exc_info is not None
        assert (await _state(event)).state == EventState.HANDLED

    @pytest.mark.parametrize("status", [RunStatus.FAILED])
    async def test_a_failed_run_replies_nothing(self, drain, client, status):
        run = await self._run(status=status)
        await self._mention(run, 5521)

        await aon_run_ended(run)

        client.create_merge_request_comment.assert_not_called()
