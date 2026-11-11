import uuid
from datetime import timedelta

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.inbox.policy import MAX_SYSTEM_RUNS_IN_A_ROW, WakePolicy
from sessions.models import (
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    QuietReason,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)

from tests.unit_tests.sessions.inbox.conftest import make_event

pytestmark = pytest.mark.django_db(transaction=True)

GATED = [EventKind.CI_FAILED, EventKind.DELEGATION_FINISHED]
UNGATED = [EventKind.MENTION, EventKind.CHAT_MESSAGE, EventKind.FOLLOW_UP_JOB]


async def _session(**fields) -> Session:
    return await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/repo", ref="main", **fields
    )


async def _runs(session: Session, *triggers: str, status: str = RunStatus.SUCCESSFUL) -> None:
    """Runs on ``session`` in the order given, oldest first."""
    start = timezone.now() - timedelta(hours=1)
    for offset, trigger in enumerate(triggers):
        await Run.objects.acreate(
            session=session,
            trigger_type=trigger,
            repo_id=session.repo_id,
            status=status,
            created_at=start + timedelta(minutes=offset),
        )


class TestConsecutiveRunLimit:
    @pytest.mark.parametrize("kind", GATED)
    async def test_it_blocks_after_the_limit_of_system_started_runs(self, kind):
        session = await _session()
        await _runs(
            session, SessionOrigin.CHAT, SessionOrigin.PIPELINE_WEBHOOK, SessionOrigin.DELEGATION_RESUME,
            SessionOrigin.PIPELINE_WEBHOOK,
        )  # fmt: skip

        assert MAX_SYSTEM_RUNS_IN_A_ROW == 3
        assert await WakePolicy(session).amay_wake(kind) == QuietReason.RUN_LIMIT

    async def test_it_allows_a_wake_below_the_limit(self):
        session = await _session()
        await _runs(session, SessionOrigin.PIPELINE_WEBHOOK, SessionOrigin.PIPELINE_WEBHOOK)

        assert await WakePolicy(session).amay_wake(EventKind.CI_FAILED) is None

    @pytest.mark.parametrize("person", [SessionOrigin.CHAT, SessionOrigin.MR_WEBHOOK])
    async def test_a_chat_message_or_mention_resets_the_count(self, person):
        session = await _session()
        await _runs(session, *[SessionOrigin.PIPELINE_WEBHOOK] * 3, person, SessionOrigin.PIPELINE_WEBHOOK)

        assert await WakePolicy(session).amay_wake(EventKind.CI_FAILED) is None

    @pytest.mark.parametrize("kind", UNGATED)
    async def test_it_never_blocks_a_persons_request(self, kind):
        session = await _session()
        await _runs(session, *[SessionOrigin.PIPELINE_WEBHOOK] * 3)

        assert await WakePolicy(session).amay_wake(kind) is None

    async def test_a_queued_next_run_is_not_counted(self):
        session = await _session()
        await _runs(session, *[SessionOrigin.PIPELINE_WEBHOOK] * 2)
        await _runs(session, SessionOrigin.PIPELINE_WEBHOOK, status=RunStatus.QUEUED)

        assert await WakePolicy(session).amay_wake(EventKind.CI_FAILED) is None


class TestQuietReasonsNow:
    @staticmethod
    def _sync_session(*, at_limit: bool = False) -> Session:
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/repo", ref="main"
        )
        for _ in range(MAX_SYSTEM_RUNS_IN_A_ROW if at_limit else 0):
            Run.objects.create(
                session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id,
                status=RunStatus.SUCCESSFUL,
            )  # fmt: skip
        return session

    def test_a_session_at_the_run_limit_quiets_a_finished_delegation(self):
        session = self._sync_session(at_limit=True)
        event = make_event(session, EventKind.DELEGATION_FINISHED)

        assert WakePolicy(session).quiet_reasons_now([event]) == {event.pk: QuietReason.RUN_LIMIT}

    def test_a_ci_failure_on_a_closed_merge_request_is_watch_off(self):
        session = self._sync_session()
        MergeRequestOwnership.objects.create(
            repo_id=session.repo_id, merge_request_iid=101, session=session, closed_at=timezone.now()
        )
        event = make_event(session, EventKind.CI_FAILED, merge_request_iid=101)

        assert WakePolicy(session).quiet_reasons_now([event]) == {event.pk: QuietReason.WATCH_OFF}

    def test_a_persons_request_is_never_quieted(self):
        session = self._sync_session(at_limit=True)
        event = make_event(session, EventKind.CHAT_MESSAGE)

        assert WakePolicy(session).quiet_reasons_now([event]) == {}

    def test_a_remembered_event_has_no_prediction(self):
        session = self._sync_session(at_limit=True)
        event = make_event(session, EventKind.COMMENT)

        assert WakePolicy(session).quiet_reasons_now([event]) == {}

    def test_it_asks_each_question_once_however_many_events(self, django_assert_num_queries):
        session = self._sync_session(at_limit=True)
        MergeRequestOwnership.objects.create(repo_id=session.repo_id, merge_request_iid=101, watch_enabled=False)
        failures = [make_event(session, EventKind.CI_FAILED, merge_request_iid=iid) for iid in (101, 102, 103)]
        delegations = [make_event(session, EventKind.DELEGATION_FINISHED) for _ in range(3)]

        with django_assert_num_queries(2):
            reasons = WakePolicy(session).quiet_reasons_now(failures + delegations)

        assert reasons[failures[0].pk] == QuietReason.WATCH_OFF
        assert {reasons[event.pk] for event in failures[1:] + delegations} == {QuietReason.RUN_LIMIT}


class TestLegFollowUp:
    async def _follow_up_run(self, session: Session, *, minutes: int) -> Run:
        run = await Run.objects.acreate(
            session=session,
            trigger_type=SessionOrigin.DELEGATED_JOB,
            repo_id=session.repo_id,
            status=RunStatus.SUCCESSFUL,
            created_at=timezone.now() + timedelta(minutes=minutes),
        )
        await InboxEvent.objects.acreate(
            session=session,
            kind=EventKind.LEG_FOLLOW_UP,
            source_key=f"leg-follow-up:{uuid.uuid4()}",
            payload={"prompt": "p", "batch_id": "b"},
            wake=True,
            state=EventState.HANDLED,
            run=run,
        )
        return run

    async def test_it_wakes_a_session_at_the_limit(self):
        session = await _session()
        await _runs(session, *[SessionOrigin.PIPELINE_WEBHOOK] * MAX_SYSTEM_RUNS_IN_A_ROW)
        pending = await sync_to_async(make_event)(
            session, EventKind.LEG_FOLLOW_UP, payload={"prompt": "p", "batch_id": "b"}
        )

        assert await sync_to_async(WakePolicy(session).quiet_reasons_now)([pending]) == {}

    async def test_its_run_counts_as_one_daiv_started(self):
        session = await _session()
        await _runs(session, SessionOrigin.CHAT, SessionOrigin.PIPELINE_WEBHOOK, SessionOrigin.PIPELINE_WEBHOOK)
        await self._follow_up_run(session, minutes=1)

        assert await sync_to_async(WakePolicy(session).system_runs_in_a_row)() == 3
        assert await WakePolicy(session).amay_wake(EventKind.CI_FAILED) == QuietReason.RUN_LIMIT

    async def test_a_legs_first_run_does_not(self):
        session = await _session()
        await _runs(
            session, SessionOrigin.DELEGATED_JOB, SessionOrigin.PIPELINE_WEBHOOK, SessionOrigin.PIPELINE_WEBHOOK
        )

        assert await sync_to_async(WakePolicy(session).system_runs_in_a_row)() == 2

    async def test_the_last_person_run_skips_its_run(self):
        session = await _session()
        await _runs(session, SessionOrigin.CHAT)
        person = await Run.objects.filter(session=session).aget()
        await self._follow_up_run(session, minutes=1)

        assert await sync_to_async(WakePolicy(session)._last_person_run_at)() == person.created_at
