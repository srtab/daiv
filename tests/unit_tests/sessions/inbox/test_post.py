import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sessions.executor.lock import stale_cutoff
from sessions.inbox import kinds
from sessions.inbox.post import apost_event
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import amake_session, chat_payload, ci_payload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def drain():
    with patch("sessions.inbox.post.drain_logged") as drain:
        yield drain


async def _post_ci(thread_id: str, *, pipeline_id: int = 1, wake: bool = True, ref: str = "", **fields):
    return await apost_event(
        thread_id,
        kind=EventKind.CI_FAILED,
        source_key=kinds.pipeline_key(pipeline_id),
        payload=ci_payload(pipeline_id),
        wake=wake,
        subject=kinds.ci_subject(9),
        ref=ref,
        merge_request_iid=9,
        **fields,
    )


class TestIdempotence:
    async def test_the_same_source_twice_inserts_one_event(self, drain):
        session = await amake_session()

        first = await _post_ci(session.thread_id)
        second = await _post_ci(session.thread_id)

        assert first.created is True
        assert second.created is False
        assert second.event.pk == first.event.pk
        assert await InboxEvent.objects.filter(session=session).acount() == 1
        drain.assert_called_once_with(session.thread_id)

    async def test_a_repeat_returns_the_run_the_event_joined(self, drain):
        session = await amake_session(active_run_id="chat-run")

        first = await _post_ci(session.thread_id)
        second = await _post_ci(session.thread_id)

        assert second.run_id == first.run_id is not None


class TestIdleSession:
    async def test_a_waking_event_drains_once_it_commits(self, drain):
        session = await amake_session()

        result = await _post_ci(session.thread_id)

        drain.assert_called_once_with(session.thread_id)
        assert result.event.run_id is None
        assert await Run.objects.filter(session=session).acount() == 0

    async def test_the_run_the_drain_starts_is_returned(self):
        session = await amake_session()
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id, status="READY"
        )

        def _drain(thread_id):
            InboxEvent.objects.filter(session_id=thread_id).update(run=run, state=EventState.TAKEN)

        with patch("sessions.inbox.post.drain_logged", side_effect=_drain):
            result = await _post_ci(session.thread_id)

        assert result.run_id == run.pk

    async def test_a_stale_holder_does_not_make_the_session_busy(self, drain):
        session = await amake_session(active_run_id="gone", last_active_at=stale_cutoff() - timedelta(minutes=1))

        result = await _post_ci(session.thread_id)

        assert result.run_id is None
        assert await Run.objects.filter(session=session).acount() == 0


class TestBusySession:
    async def test_a_waking_event_creates_the_next_run_for_its_branch(self, drain):
        session = await amake_session(active_run_id="chat-run")

        result = await _post_ci(session.thread_id)

        run = await Run.objects.aget(pk=result.run_id)
        assert run.status == RunStatus.QUEUED
        assert run.trigger_type == SessionOrigin.PIPELINE_WEBHOOK
        assert run.ref == session.ref
        assert run.task_result_id is None
        assert result.event.run_id == run.pk
        drain.assert_called_once_with(session.thread_id)

    async def test_waking_events_on_one_branch_join_the_same_next_run(self, drain):
        session = await amake_session(active_run_id="chat-run")

        first = await _post_ci(session.thread_id, pipeline_id=1)
        chat = await apost_event(
            session.thread_id, kind=EventKind.CHAT_MESSAGE, source_key="chat:m1", payload=chat_payload(), wake=True
        )

        assert chat.run_id == first.run_id
        assert await Run.objects.filter(session=session).acount() == 1

    async def test_the_next_run_shows_what_waits_for_it(self, drain):
        session = await amake_session(active_run_id="chat-run")

        first = await _post_ci(session.thread_id, pipeline_id=1)
        await apost_event(
            session.thread_id,
            kind=EventKind.CHAT_MESSAGE,
            source_key="chat:m1",
            payload=chat_payload("Also add a test"),
            wake=True,
        )

        run = await Run.objects.aget(pk=first.run_id)
        assert run.trigger_type == SessionOrigin.CHAT
        assert run.prompt.startswith("Also add a test\n\nCI failed on this merge request.")

    async def test_events_on_different_branches_get_different_next_runs(self, drain):
        session = await amake_session(active_run_id="chat-run")

        here = await _post_ci(session.thread_id, pipeline_id=1)
        there = await _post_ci(session.thread_id, pipeline_id=2, ref="feat/other")

        assert here.run_id != there.run_id
        assert await Run.objects.filter(pk=there.run_id, ref="feat/other").aexists()

    async def test_a_queued_next_run_is_joined_even_once_the_slot_frees(self, drain):
        session = await amake_session(active_run_id="chat-run")
        first = await _post_ci(session.thread_id, pipeline_id=1)
        await Session.objects.filter(pk=session.pk).aupdate(active_run_id=None)

        second = await _post_ci(session.thread_id, pipeline_id=2)

        assert second.run_id == first.run_id

    async def test_a_person_who_asked_starts_the_next_run(self, drain, member_user):
        session = await amake_session(active_run_id="chat-run")

        result = await apost_event(
            session.thread_id,
            kind=EventKind.FOLLOW_UP_JOB,
            source_key=kinds.job_key(uuid.uuid4()),
            payload={"prompt": "Bump it", "trigger_type": SessionOrigin.MCP_JOB},
            wake=True,
            author=member_user,
        )

        run = await Run.objects.aget(pk=result.run_id)
        assert run.trigger_type == SessionOrigin.MCP_JOB
        assert run.user_id == member_user.pk


class TestRememberedEvents:
    async def test_a_remembered_event_never_drains_nor_joins_a_run(self, drain):
        session = await amake_session(active_run_id="chat-run")

        result = await apost_event(
            session.thread_id,
            kind=EventKind.CI_PASSED,
            source_key=kinds.pipeline_key(3),
            payload=ci_payload(3, status="success"),
            wake=False,
            subject=kinds.ci_subject(9),
        )

        drain.assert_not_called()
        assert result.run_id is None
        assert result.event.ref == ""

    async def test_a_quiet_ci_failure_records_its_reason(self, drain):
        session = await amake_session()

        result = await _post_ci(session.thread_id, wake=False, reason="already_examined")

        assert result.event.reason == "already_examined"
        drain.assert_not_called()


class TestRefusals:
    async def test_an_unknown_session_raises(self, drain):
        with pytest.raises(Session.DoesNotExist):
            await _post_ci(str(uuid.uuid4()))

    async def test_an_invalid_payload_raises(self, drain):
        session = await amake_session()

        with pytest.raises(ValueError, match="missing"):
            await apost_event(session.thread_id, kind=EventKind.CI_FAILED, source_key="k", payload={}, wake=True)

    async def test_a_kind_that_never_wakes_cannot_be_posted_waking(self, drain):
        session = await amake_session()

        with pytest.raises(ValueError, match="never wakes"):
            await apost_event(
                session.thread_id, kind=EventKind.NOTICE, source_key="k", payload={"text": "t"}, wake=True
            )
