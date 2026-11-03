from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.executor.lock import stale_cutoff
from sessions.inbox.complete import ANSWERED_IN_ANOTHER_THREAD, MAX_EVENT_ATTEMPTS, MergeRequestReplies, RunEnd
from sessions.inbox.drain import Drain
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.queue import ENQUEUE_FAILED_PREFIX

from codebase.base import GitPlatform
from codebase.clients.base import CommentThread
from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session, mention_payload

pytestmark = pytest.mark.django_db(transaction=True)

CI_SUMMARY = "The failure is a GitHub auth error, not the change; I pushed nothing."


@pytest.fixture
def drain():
    with patch.object(Drain, "start_logged", autospec=True) as drain:
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

        await RunEnd(run).aon_run_ended()

        assert (await _state(event)).state == EventState.HANDLED
        assert [call.args[0].thread_id for call in drain.call_args_list] == [run.session_id]

    async def test_a_run_its_owner_stopped_dismisses_its_events(self, drain):
        run, event = await _run_with_event(RunStatus.FAILED, cancel_requested_at=timezone.now())

        await RunEnd(run).aon_run_ended()

        assert (await _state(event)).state == EventState.DISMISSED
        assert [call.args[0].thread_id for call in drain.call_args_list] == [run.session_id]

    async def test_a_failed_run_puts_its_events_back_for_another_attempt(self, drain):
        run, event = await _run_with_event(RunStatus.FAILED)

        await RunEnd(run).aon_run_ended()

        stored = await _state(event)
        assert (stored.state, stored.attempts, stored.run_id) == (EventState.PENDING, 1, None)
        assert stored.payload["tried_runs"] == [str(run.pk)]
        assert [call.args[0].thread_id for call in drain.call_args_list] == [run.session_id]

    async def test_the_last_attempt_fails_the_events_and_logs(self, drain, caplog):
        run, event = await _run_with_event(RunStatus.FAILED, attempts=MAX_EVENT_ATTEMPTS - 1)

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            await RunEnd(run).aon_run_ended()

        stored = await _state(event)
        assert (stored.state, stored.failure) == (EventState.FAILED, EventFailure.RETRIES_EXHAUSTED)
        assert stored.attempts == MAX_EVENT_ATTEMPTS
        assert "failed after 3 attempts" in caplog.text
        assert [call.args[0].thread_id for call in drain.call_args_list] == [run.session_id]

    async def test_a_ci_failure_no_run_could_examine_leaves_its_commit_unexamined(self, drain):
        """So a re-run pipeline on that commit can wake the session again."""
        run, event = await _run_with_event(RunStatus.FAILED, attempts=MAX_EVENT_ATTEMPTS - 1)
        sha = event.payload["sha"]
        await InboxEvent.objects.filter(pk=event.pk).aupdate(merge_request_iid=9)
        ownership = await MergeRequestOwnership.objects.acreate(
            repo_id=run.repo_id, merge_request_iid=9, session_id=run.session_id, source_branch="x", attempts=1,
            last_examined_sha=sha,
        )  # fmt: skip

        await RunEnd(run).aon_run_ended()

        await ownership.arefresh_from_db()
        assert (ownership.attempts, ownership.last_examined_sha) == (1, "")

    async def test_a_run_whose_drain_failed_to_enqueue_it_spends_an_attempt_and_leaves_the_drain_to_the_sweep(
        self, drain
    ):
        run, event = await _run_with_event(
            RunStatus.FAILED, error_message=f"{ENQUEUE_FAILED_PREFIX}: RuntimeError: broker down"
        )

        await RunEnd(run).aon_run_ended(drain=False)

        stored = await _state(event)
        assert (stored.state, stored.attempts, stored.run_id) == (EventState.PENDING, 1, None)
        drain.assert_not_called()

    async def test_events_the_run_did_not_take_are_left_alone(self, drain):
        run, _ = await _run_with_event(RunStatus.FAILED)
        pending = await amake_event(await Session.objects.aget(pk=run.session_id), EventKind.NOTICE, run=run)

        await RunEnd(run).aon_run_ended()

        assert (await _state(pending)).attempts == 0

    async def test_a_settling_failure_is_logged_and_still_drains(self, drain, caplog):
        run, _ = await _run_with_event(RunStatus.SUCCESSFUL)

        with (
            patch.object(RunEnd, "settle_events", side_effect=RuntimeError("db down")),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            await RunEnd(run).aon_run_ended()

        assert "settling the events" in caplog.text
        assert [call.args[0].thread_id for call in drain.call_args_list] == [run.session_id]


class TestSlot:
    async def test_a_run_stopped_before_its_task_ran_frees_the_slot_the_drain_claimed(self, drain):
        run, event = await _run_with_event(RunStatus.READY)
        await Session.objects.filter(pk=run.session_id).aupdate(active_run_id=str(run.pk))

        assert await sync_to_async(Run.objects.cancel)(run) is True

        assert (await Session.objects.aget(pk=run.session_id)).active_run_id is None
        assert (await _state(event)).state == EventState.DISMISSED
        assert [call.args[0].thread_id for call in drain.call_args_list] == [run.session_id]

    async def test_another_holders_slot_is_left_alone(self, drain):
        run, _ = await _run_with_event(RunStatus.FAILED)
        await Session.objects.filter(pk=run.session_id).aupdate(active_run_id="chat-run")

        await RunEnd(run).aon_run_ended()

        assert (await Session.objects.aget(pk=run.session_id)).active_run_id == "chat-run"


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
    return await sync_to_async(RunEnd.recover_stale_events)()


class TestOwnerlessCiReport:
    @pytest.fixture
    def client(self):
        client = MagicMock()
        client.current_user.username = "daiv"
        with patch("codebase.clients.RepoClient.create_instance", return_value=client):
            yield client

    async def _ci_run(
        self, *, user=None, status=RunStatus.SUCCESSFUL, wake=True, attempts=0, summary=CI_SUMMARY, **run_fields
    ) -> Run:
        session = await amake_session(user=user)
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id, status=status,
            result_summary=summary, **run_fields,
        )  # fmt: skip
        await amake_event(
            session, EventKind.CI_FAILED, run=run, state=EventState.TAKEN, merge_request_iid=9, wake=wake,
            attempts=attempts,
        )  # fmt: skip
        return run

    async def test_a_session_without_a_user_gets_the_summary_on_the_merge_request(self, drain, client):
        run = await self._ci_run()

        await RunEnd(run).aon_run_ended()

        client.create_merge_request_comment.assert_called_once_with("group/repo", 9, CI_SUMMARY)

    async def test_a_question_goes_on_the_merge_request_with_how_to_answer_it(self, drain, client):
        run = await self._ci_run(status=RunStatus.WAITING_INPUT, summary="Should I pin the auth action instead?")

        await RunEnd(run).aon_run_ended()

        (repo_id, iid, body), _ = client.create_merge_request_comment.call_args
        assert (repo_id, iid) == ("group/repo", 9)
        assert body.startswith("Should I pin the auth action instead?")
        assert body.endswith("\n\nReply mentioning @daiv with your answer.")

    async def test_a_ci_failure_no_run_could_fix_gets_the_failure_note_once_it_fails_for_good(self, drain, client):
        run = await self._ci_run(
            status=RunStatus.FAILED, attempts=MAX_EVENT_ATTEMPTS - 1, error_message="RuntimeError: token=s3cr3t"
        )

        await RunEnd(run).aon_run_ended()

        client.create_merge_request_comment.assert_called_once()
        (repo_id, iid, body), _ = client.create_merge_request_comment.call_args
        assert (repo_id, iid) == ("group/repo", 9)
        assert "An unexpected error occurred" in body
        assert "s3cr3t" not in body

    @pytest.mark.parametrize(
        ("owned", "status", "wake", "attempts"),
        [
            (True, RunStatus.SUCCESSFUL, True, 0),
            (True, RunStatus.FAILED, True, MAX_EVENT_ATTEMPTS - 1),
            (False, RunStatus.SUCCESSFUL, False, 0),
            (False, RunStatus.FAILED, True, 0),
        ],
        ids=["owned", "owned-and-failed-for-good", "not-woken-by-ci", "failed-with-attempts-left"],
    )
    async def test_otherwise_nothing_is_posted(self, drain, client, member_user, owned, status, wake, attempts):
        run = await self._ci_run(user=member_user if owned else None, status=status, wake=wake, attempts=attempts)

        await RunEnd(run).aon_run_ended()

        client.create_merge_request_comment.assert_not_called()

    @pytest.mark.parametrize(
        ("status", "attempts", "state"),
        [
            (RunStatus.SUCCESSFUL, 0, EventState.HANDLED),
            (RunStatus.WAITING_INPUT, 0, EventState.HANDLED),
            (RunStatus.FAILED, MAX_EVENT_ATTEMPTS - 1, EventState.FAILED),
        ],
        ids=["summary", "question", "failure-note"],
    )
    async def test_a_failed_post_is_logged_and_changes_no_event(self, drain, client, caplog, status, attempts, state):
        run = await self._ci_run(status=status, attempts=attempts)
        client.create_merge_request_comment.side_effect = RuntimeError("platform down")

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            await RunEnd(run).aon_run_ended()

        assert "could not comment on" in caplog.text
        assert caplog.records[-1].exc_info is not None
        assert (
            await InboxEvent.objects.filter(state=state, attempts=attempts + (status == RunStatus.FAILED)).acount() == 1
        )

    def test_one_failed_comment_does_not_stop_the_others(self, client, caplog):
        client.create_merge_request_comment.side_effect = [RuntimeError("platform down"), None]

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            MergeRequestReplies.comment_on_merge_requests("group/repo", [2, 1], lambda replies: "body")

        assert [call.args[1] for call in client.create_merge_request_comment.call_args_list] == [1, 2]
        assert "could not comment on group/repo!1" in caplog.text


class TestDelegatedNoPush:
    async def _ci_run(
        self, *, pushed_sha: str = "", parent: bool = True, status=RunStatus.SUCCESSFUL, attempts: int = 0
    ) -> Run:
        coordinator = await amake_session(repo_id="group/coordinator") if parent else None
        session = await amake_session(parent=coordinator)
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id, status=status,
            pushed_sha=pushed_sha,
        )  # fmt: skip
        await amake_event(
            session, EventKind.CI_FAILED, run=run, state=EventState.TAKEN, merge_request_iid=9, attempts=attempts
        )
        return run

    async def test_a_ci_run_that_pushed_nothing_is_remembered_by_the_parent(self, drain):
        run = await self._ci_run()
        session = await Session.objects.aget(pk=run.session_id)

        await RunEnd(run).aon_run_ended()

        event = await InboxEvent.objects.aget(session_id=session.parent_id)
        assert (event.kind, event.wake, event.source_key) == (
            EventKind.DELEGATED_CI_OUTCOME, False, f"child-ci:{session.thread_id}:830724:no_push",
        )  # fmt: skip
        assert event.payload == {
            "child_thread_id": session.thread_id,
            "merge_request_iid": 9,
            "outcome": "no_push",
            "web_url": "https://gitlab.example/pipelines/830724",
        }

    async def test_a_ci_run_that_asked_a_question_and_pushed_nothing_is_remembered_by_the_parent(self, drain):
        run = await self._ci_run(status=RunStatus.WAITING_INPUT)
        session = await Session.objects.aget(pk=run.session_id)

        await RunEnd(run).aon_run_ended()

        event = await InboxEvent.objects.aget(session_id=session.parent_id)
        assert (event.kind, event.payload["outcome"]) == (EventKind.DELEGATED_CI_OUTCOME, "no_push")

    async def test_a_ci_failure_no_run_could_fix_is_remembered_by_the_parent_once_it_fails_for_good(self, drain):
        run = await self._ci_run(status=RunStatus.FAILED, attempts=MAX_EVENT_ATTEMPTS - 1)
        session = await Session.objects.aget(pk=run.session_id)

        await RunEnd(run).aon_run_ended()

        event = await InboxEvent.objects.aget(session_id=session.parent_id)
        assert (event.kind, event.source_key) == (
            EventKind.DELEGATED_CI_OUTCOME, f"child-ci:{session.thread_id}:830724:no_push",
        )  # fmt: skip

    async def test_a_ci_failure_whose_run_died_with_its_slot_is_remembered_by_the_parent_once_it_fails_for_good(self):
        run = await self._ci_run(status=RunStatus.RUNNING, attempts=MAX_EVENT_ATTEMPTS - 1)
        session = await Session.objects.aget(pk=run.session_id)
        await Session.objects.filter(pk=run.session_id).aupdate(
            active_run_id=str(run.pk), last_active_at=stale_cutoff() - timedelta(minutes=1)
        )

        with patch.object(Drain, "start_logged"):
            await _recover()

        event = await InboxEvent.objects.aget(session_id=session.parent_id)
        assert (event.kind, event.payload["outcome"]) == (EventKind.DELEGATED_CI_OUTCOME, "no_push")

    @pytest.mark.parametrize(
        ("fields"),
        [
            {"pushed_sha": "f00dfeed"},
            {"pushed_sha": "f00dfeed", "status": RunStatus.WAITING_INPUT},
            {"parent": False},
            {"status": RunStatus.FAILED},
            {"status": RunStatus.FAILED, "attempts": MAX_EVENT_ATTEMPTS - 1, "pushed_sha": "f00dfeed"},
            {"status": RunStatus.FAILED, "attempts": MAX_EVENT_ATTEMPTS - 1, "parent": False},
        ],
        ids=[
            "pushed",
            "asked-after-pushing",
            "no-parent",
            "failed",
            "failed-for-good-after-pushing",
            "failed-for-good-without-a-parent",
        ],
    )
    async def test_otherwise_no_parent_hears_of_it(self, drain, fields):
        run = await self._ci_run(**fields)

        await RunEnd(run).aon_run_ended()

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

    async def _mention(self, run, note_id, *, iid=9, discussion="d", is_diff=False, **fields):
        payload = mention_payload(note_id=note_id) | {"discussion_id": f"{discussion}{note_id}", "is_diff": is_diff}
        return await amake_event(
            await Session.objects.aget(pk=run.session_id), EventKind.MENTION, run=run, state=EventState.TAKEN,
            merge_request_iid=iid, payload=payload, **fields,
        )  # fmt: skip

    async def test_one_mention_gets_the_final_message_in_its_thread(self, drain, client):
        run = await self._run()
        await self._mention(run, 5521, is_diff=True)

        await RunEnd(run).aon_run_ended()

        client.create_merge_request_comment.assert_called_once_with(
            "group/repo", 9, "Renamed it in the three call sites.", reply_to=CommentThread("d5521", on_diff=True)
        )

    async def test_a_question_tells_the_asker_how_to_answer(self, drain, client):
        client.current_user.username = "daiv"
        run = await self._run(status=RunStatus.WAITING_INPUT, summary="Which branch should I target?")
        await self._mention(run, 5521, author_username="alice")

        await RunEnd(run).aon_run_ended()

        body = client.create_merge_request_comment.call_args.args[2]
        assert body.startswith("Which branch should I target?")
        assert body.endswith("@alice, reply mentioning @daiv with your answer.")

    async def test_a_run_that_said_nothing_posts_that_it_could_not_address_the_mention(self, drain, client):
        run = await self._run(summary="")
        await self._mention(run, 5521)

        await RunEnd(run).aon_run_ended()

        assert "An unexpected error occurred" in client.create_merge_request_comment.call_args.args[2]

    async def test_a_mention_whose_runs_kept_failing_is_told_once_it_fails_for_good(self, drain, client):
        run = await self._run(status=RunStatus.FAILED)
        event = await self._mention(run, 5521)
        await InboxEvent.objects.filter(pk=event.pk).aupdate(attempts=MAX_EVENT_ATTEMPTS - 1)

        await RunEnd(run).aon_run_ended()

        assert (await _state(event)).failure == EventFailure.RETRIES_EXHAUSTED
        client.create_merge_request_comment.assert_called_once()
        args, kwargs = client.create_merge_request_comment.call_args
        assert (args[:2], kwargs["reply_to"]) == (("group/repo", 9), CommentThread("d5521", on_diff=False))
        assert "An unexpected error occurred" in args[2]

    async def test_several_mentions_get_the_reply_in_the_latest_thread_and_a_link_to_it_in_the_others(
        self, drain, client
    ):
        client.git_platform = GitPlatform.GITLAB
        client.create_merge_request_comment.side_effect = [701, 702, 703]
        run = await self._run()
        await self._mention(run, 1)
        await self._mention(run, 2)
        await self._mention(run, 3, iid=10)

        await RunEnd(run).aon_run_ended()

        calls = [
            (c.args[1], c.args[2], c.kwargs["reply_to"].id) for c in client.create_merge_request_comment.call_args_list
        ]
        assert calls == [
            (9, "Renamed it in the three call sites.", "d2"),
            (9, ANSWERED_IN_ANOTHER_THREAD.format(url="https://gitlab.example/mr/9#note_701"), "d1"),
            (10, "Renamed it in the three call sites.", "d3"),
        ]

    @pytest.mark.parametrize(
        ("is_diff", "web_url", "link"),
        [
            (False, "https://github.com/o/r/pull/9#issuecomment-2", "https://github.com/o/r/pull/9#issuecomment-701"),
            (True, "https://github.com/o/r/pull/9#discussion_r2", "https://github.com/o/r/pull/9#discussion_r701"),
        ],
        ids=["conversation", "review-thread"],
    )
    async def test_on_github_the_link_names_the_reply_by_its_kind_of_comment(
        self, drain, client, is_diff, web_url, link
    ):
        client.git_platform = GitPlatform.GITHUB
        client.create_merge_request_comment.side_effect = [701, 702]
        run = await self._run()
        await self._mention(run, 1, is_diff=is_diff)
        latest = await self._mention(run, 2, is_diff=is_diff)
        await InboxEvent.objects.filter(pk=latest.pk).aupdate(payload=latest.payload | {"web_url": web_url})

        await RunEnd(run).aon_run_ended()

        assert client.create_merge_request_comment.call_args.args[2] == ANSWERED_IN_ANOTHER_THREAD.format(url=link)

    async def test_without_the_replys_id_the_link_points_at_the_latest_mention(self, drain, client):
        client.git_platform = GitPlatform.GITLAB
        client.create_merge_request_comment.return_value = None
        run = await self._run()
        await self._mention(run, 1)
        await self._mention(run, 2)

        await RunEnd(run).aon_run_ended()

        body = client.create_merge_request_comment.call_args.args[2]
        assert body == ANSWERED_IN_ANOTHER_THREAD.format(url="https://gitlab.example/mr/9#2")

    async def test_a_failed_post_is_logged_and_the_events_stay_handled(self, drain, client, caplog):
        run = await self._run()
        event = await self._mention(run, 5521)
        client.create_merge_request_comment.side_effect = RuntimeError("platform down")

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            await RunEnd(run).aon_run_ended()

        assert "could not reply to the mentions" in caplog.text
        assert caplog.records[-1].exc_info is not None
        assert (await _state(event)).state == EventState.HANDLED

    @pytest.mark.parametrize("status", [RunStatus.FAILED])
    async def test_a_failed_run_replies_nothing(self, drain, client, status):
        run = await self._run(status=status)
        await self._mention(run, 5521)

        await RunEnd(run).aon_run_ended()

        client.create_merge_request_comment.assert_not_called()
