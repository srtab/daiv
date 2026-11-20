import uuid

import pytest
from sessions.inbox.timeline import Timeline
from sessions.models import EventKind, EventState, Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import make_event, mention_payload


@pytest.mark.django_db
class TestTimeline:
    @staticmethod
    def _session() -> Session:
        return Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="g/r", ref="x")

    @staticmethod
    def _run(session: Session, status: str = RunStatus.SUCCESSFUL) -> Run:
        return Run.objects.create(
            session=session, trigger_type=SessionOrigin.CHAT, repo_id=session.repo_id, status=status
        )

    def test_events_leave_out_pending_and_superseded_and_keep_failed(self):
        session = self._session()
        make_event(session, EventKind.COMMENT)
        make_event(session, EventKind.CI_FAILED, wake=False, state=EventState.SUPERSEDED)
        handled = make_event(session, EventKind.COMMENT, state=EventState.HANDLED, run=self._run(session))
        failed = make_event(session, EventKind.MENTION, state=EventState.FAILED, failure="retries_exhausted")

        assert Timeline(session.thread_id).events() == [handled, failed]

    def test_opening_events_are_the_waking_events_runs_took_or_tried(self):
        session = self._session()
        chat = make_event(session, EventKind.CHAT_MESSAGE, state=EventState.HANDLED, run=self._run(session))
        failed = make_event(session, EventKind.MENTION, state=EventState.FAILED, run=self._run(session))
        retried = make_event(session, EventKind.MENTION, payload={**mention_payload(), "tried_runs": ["r1"]})
        make_event(session, EventKind.CHAT_MESSAGE)

        assert Timeline(session.thread_id).opening_events() == [chat, failed, retried]

    def test_an_event_opens_the_runs_that_tried_it_then_the_one_holding_it(self):
        session = self._session()
        run = self._run(session)
        event = make_event(
            session, EventKind.MENTION, state=EventState.TAKEN, run=run,
            payload={**mention_payload(), "tried_runs": ["r1", "r2"]},
        )  # fmt: skip
        pending = make_event(session, EventKind.CHAT_MESSAGE, run=run)

        assert (Timeline.opened(event), Timeline.opened(pending)) == (["r1", "r2", str(run.pk)], [])
