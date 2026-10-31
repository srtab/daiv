import uuid
from datetime import timedelta

from django.utils import timezone

import pytest
from sessions.inbox.policy import MAX_SYSTEM_RUNS_IN_A_ROW, amay_wake
from sessions.models import EventKind, QuietReason, Run, RunStatus, Session, SessionOrigin

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


class TestMute:
    @pytest.mark.parametrize("kind", GATED)
    async def test_it_blocks_ci_and_delegation_wakes(self, kind):
        session = await _session(inbox_muted=True)

        assert await amay_wake(session, kind) == QuietReason.MUTED

    @pytest.mark.parametrize("kind", UNGATED)
    async def test_it_never_blocks_a_persons_request(self, kind):
        session = await _session(inbox_muted=True)

        assert await amay_wake(session, kind) is None

    async def test_an_unmuted_session_wakes(self):
        assert await amay_wake(await _session(), EventKind.CI_FAILED) is None


class TestConsecutiveRunLimit:
    @pytest.mark.parametrize("kind", GATED)
    async def test_it_blocks_after_the_limit_of_system_started_runs(self, kind):
        session = await _session()
        await _runs(
            session, SessionOrigin.CHAT, SessionOrigin.PIPELINE_WEBHOOK, SessionOrigin.DELEGATION_RESUME,
            SessionOrigin.PIPELINE_WEBHOOK,
        )  # fmt: skip

        assert MAX_SYSTEM_RUNS_IN_A_ROW == 3
        assert await amay_wake(session, kind) == QuietReason.RUN_LIMIT

    async def test_it_allows_a_wake_below_the_limit(self):
        session = await _session()
        await _runs(session, SessionOrigin.PIPELINE_WEBHOOK, SessionOrigin.PIPELINE_WEBHOOK)

        assert await amay_wake(session, EventKind.CI_FAILED) is None

    @pytest.mark.parametrize("person", [SessionOrigin.CHAT, SessionOrigin.MR_WEBHOOK])
    async def test_a_chat_message_or_mention_resets_the_count(self, person):
        session = await _session()
        await _runs(session, *[SessionOrigin.PIPELINE_WEBHOOK] * 3, person, SessionOrigin.PIPELINE_WEBHOOK)

        assert await amay_wake(session, EventKind.CI_FAILED) is None

    @pytest.mark.parametrize("kind", UNGATED)
    async def test_it_never_blocks_a_persons_request(self, kind):
        session = await _session()
        await _runs(session, *[SessionOrigin.PIPELINE_WEBHOOK] * 3)

        assert await amay_wake(session, kind) is None

    async def test_a_queued_next_run_is_not_counted(self):
        session = await _session()
        await _runs(session, *[SessionOrigin.PIPELINE_WEBHOOK] * 2)
        await _runs(session, SessionOrigin.PIPELINE_WEBHOOK, status=RunStatus.QUEUED)

        assert await amay_wake(session, EventKind.CI_FAILED) is None
