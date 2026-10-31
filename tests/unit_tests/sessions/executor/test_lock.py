import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.executor.lock import (
    Claimed,
    Held,
    LiveRun,
    NoLock,
    SessionActivity,
    SessionLock,
    SessionLockLostError,
    SessionLockTimeoutError,
    Wait,
    background_in_flight,
    hold_session_lock,
    live_run_for,
    session_activity,
    stale_cutoff,
    still_held,
)
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User
from tests.unit_tests.sessions.conftest import active_holder, amake_job_session

pytestmark = pytest.mark.django_db(transaction=True)


def _run(trigger_type, status=RunStatus.RUNNING):
    return SimpleNamespace(trigger_type=trigger_type, status=status)


class TestBackgroundInFlight:
    @pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.READY, RunStatus.RUNNING])
    def test_a_live_non_chat_run_is_background_work(self, status):
        runs = [_run(SessionOrigin.CHAT, RunStatus.SUCCESSFUL), _run(SessionOrigin.DELEGATION_RESUME, status)]

        assert background_in_flight(SessionActivity(in_flight=True), runs) is True

    def test_a_chat_run_alone_is_not(self):
        assert background_in_flight(SessionActivity(in_flight=True), [_run(SessionOrigin.CHAT)]) is False

    def test_a_finished_non_chat_run_is_not(self):
        runs = [_run(SessionOrigin.PIPELINE_WEBHOOK, RunStatus.SUCCESSFUL), _run(SessionOrigin.CHAT)]

        assert background_in_flight(SessionActivity(in_flight=True), runs) is False

    def test_a_session_out_of_flight_is_not(self):
        assert background_in_flight(SessionActivity(), [_run(SessionOrigin.DELEGATION_RESUME)]) is False


class TestSessionLock:
    async def test_it_claims_a_free_slot(self):
        thread_id = await amake_job_session()

        assert await SessionLock.try_claim(thread_id, "run-1") is True
        assert await active_holder(thread_id) == "run-1"

    async def test_it_refuses_a_live_holders_slot(self):
        thread_id = await amake_job_session(active_run_id="run-1")

        assert await SessionLock.try_claim(thread_id, "run-2") is False

    async def test_it_takes_over_a_stale_holders_slot(self):
        stale = stale_cutoff() - timedelta(minutes=1)
        thread_id = await amake_job_session(active_run_id="run-1", last_active_at=stale)

        assert await SessionLock.try_claim(thread_id, "run-2") is True
        assert await active_holder(thread_id) == "run-2"

    async def test_only_the_holder_releases(self):
        thread_id = await amake_job_session(active_run_id="run-1")

        await SessionLock.release(thread_id, "run-2")
        assert await active_holder(thread_id) == "run-1"

        await SessionLock.release(thread_id, "run-1")
        assert await active_holder(thread_id) is None

    async def test_only_the_holder_heartbeats(self):
        old = timezone.now() - timedelta(minutes=10)
        thread_id = await amake_job_session(active_run_id="run-1", last_active_at=old)

        assert await SessionLock.heartbeat(thread_id, "run-2") is False
        assert (await Session.objects.aget(pk=thread_id)).last_active_at == old

        assert await SessionLock.heartbeat(thread_id, "run-1") is True
        assert (await Session.objects.aget(pk=thread_id)).last_active_at > old


class TestWait:
    async def test_it_holds_the_slot_for_the_body_and_frees_it_after(self):
        thread_id = await amake_job_session()

        async with hold_session_lock(Wait(holder_id="run-1", timeout_s=1), thread_id):
            assert await active_holder(thread_id) == "run-1"

        assert await active_holder(thread_id) is None

    async def test_it_waits_for_a_held_slot_and_claims_it_once_freed(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        real_try_claim = SessionLock.try_claim
        attempts: list[bool] = []

        async def _try_claim(thread_id, holder_id):
            claimed = await real_try_claim(thread_id, holder_id)
            attempts.append(claimed)
            if not claimed:
                await SessionLock.release(thread_id, "chat-run")
            return claimed

        with (
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.try_claim", _try_claim),
        ):
            async with hold_session_lock(Wait(holder_id="run-1", timeout_s=5), thread_id):
                assert await active_holder(thread_id) == "run-1"

        assert attempts == [False, True]

    async def test_it_logs_the_wait_once_a_claim_succeeds_after_polling(self, caplog):
        thread_id = await amake_job_session(active_run_id="chat-run")
        real_try_claim = SessionLock.try_claim

        async def _try_claim(thread_id, holder_id):
            claimed = await real_try_claim(thread_id, holder_id)
            if not claimed:
                await SessionLock.release(thread_id, "chat-run")
            return claimed

        with (
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.try_claim", _try_claim),
            caplog.at_level("INFO", logger="daiv.sessions"),
        ):
            async with hold_session_lock(Wait(holder_id="run-1", timeout_s=5), thread_id):
                pass

        assert f"thread_id={thread_id}" in caplog.text
        assert "run-1" in caplog.text
        assert "waited" in caplog.text

    async def test_an_immediate_claim_logs_no_wait(self, caplog):
        thread_id = await amake_job_session()

        with caplog.at_level("INFO", logger="daiv.sessions"):
            async with hold_session_lock(Wait(holder_id="run-1", timeout_s=1), thread_id):
                pass

        assert "waited" not in caplog.text

    async def test_it_times_out_when_the_slot_never_frees(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        body = AsyncMock()

        with (
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            pytest.raises(SessionLockTimeoutError, match="not released within"),
        ):
            async with hold_session_lock(Wait(holder_id="run-1", timeout_s=0.05), thread_id):
                await body()

        body.assert_not_awaited()
        assert await active_holder(thread_id) == "chat-run"

    async def test_a_thread_without_a_session_row_fails_instead_of_waiting(self):
        body = AsyncMock()

        with patch("sessions.executor.lock.asyncio.sleep") as sleep, pytest.raises(Session.DoesNotExist):
            async with hold_session_lock(Wait(holder_id="run-1", timeout_s=60), "t-gone"):
                await body()

        body.assert_not_awaited()
        sleep.assert_not_called()


class TestHeld:
    async def test_it_keeps_the_callers_claim_and_leaves_its_release_to_the_caller(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        try_claim = AsyncMock()

        with patch("sessions.executor.lock.SessionLock.try_claim", try_claim):
            async with hold_session_lock(Held(holder_id="chat-run"), thread_id):
                pass

        try_claim.assert_not_awaited()
        assert await active_holder(thread_id) == "chat-run"

    async def test_it_heartbeats_the_callers_claim(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        beats: list[str] = []
        beat = asyncio.Event()

        async def _heartbeat(thread_id, holder_id):
            beats.append(holder_id)
            beat.set()
            return True

        with (
            patch("sessions.executor.lock.LOCK_HEARTBEAT_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.heartbeat", _heartbeat),
        ):
            async with hold_session_lock(Held(holder_id="chat-run"), thread_id):
                await asyncio.wait_for(beat.wait(), timeout=5)

        assert beats[0] == "chat-run"


class TestClaimed:
    async def test_it_runs_on_the_drainers_claim_and_frees_it_after(self):
        thread_id = await amake_job_session(active_run_id="run-1")
        try_claim = AsyncMock()

        with patch("sessions.executor.lock.SessionLock.try_claim", try_claim):
            async with hold_session_lock(Claimed(holder_id="run-1"), thread_id) as holder_id:
                assert holder_id == "run-1"
                assert await active_holder(thread_id) == "run-1"

        try_claim.assert_not_awaited()
        assert await active_holder(thread_id) is None

    async def test_it_heartbeats_the_claim(self):
        thread_id = await amake_job_session(active_run_id="run-1")
        beats: list[str] = []
        beat = asyncio.Event()

        async def _heartbeat(thread_id, holder_id):
            beats.append(holder_id)
            beat.set()
            return True

        with (
            patch("sessions.executor.lock.LOCK_HEARTBEAT_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.heartbeat", _heartbeat),
        ):
            async with hold_session_lock(Claimed(holder_id="run-1"), thread_id):
                await asyncio.wait_for(beat.wait(), timeout=5)

        assert set(beats) == {"run-1"}

    async def test_a_claim_lost_before_the_run_starts_fails_without_touching_the_new_holder(self):
        thread_id = await amake_job_session(active_run_id="run-2")

        with pytest.raises(SessionLockLostError):
            async with hold_session_lock(Claimed(holder_id="run-1"), thread_id):
                pytest.fail("the body must not run without the slot")

        assert await active_holder(thread_id) == "run-2"

    async def test_a_claim_taken_over_mid_run_is_left_to_its_new_holder(self):
        thread_id = await amake_job_session(active_run_id="run-1")

        async with hold_session_lock(Claimed(holder_id="run-1"), thread_id):
            await Session.objects.filter(thread_id=thread_id).aupdate(active_run_id="run-2")

        assert await active_holder(thread_id) == "run-2"


class TestClaimForRun:
    async def test_it_claims_a_free_slot_for_the_run(self):
        thread_id = await amake_job_session()

        assert await sync_to_async(SessionLock.claim_for_run)(thread_id, "run-1") is True
        assert await active_holder(thread_id) == "run-1"

    async def test_it_refuses_a_live_holders_slot(self):
        thread_id = await amake_job_session(active_run_id="chat-run")

        assert await sync_to_async(SessionLock.claim_for_run)(thread_id, "run-1") is False
        assert await active_holder(thread_id) == "chat-run"

    async def test_it_takes_over_a_stale_slot_and_says_so(self, caplog):
        stale = stale_cutoff() - timedelta(minutes=1)
        thread_id = await amake_job_session(active_run_id="chat-run", last_active_at=stale)

        with caplog.at_level("WARNING", logger="daiv.sessions"):
            assert await sync_to_async(SessionLock.claim_for_run)(thread_id, "run-1") is True

        assert await active_holder(thread_id) == "run-1"
        assert "stale takeover" in caplog.text


class TestNoLock:
    async def test_it_never_touches_the_session_slot(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        heartbeats: list[tuple] = []

        async def _loop(*args):
            heartbeats.append(args)

        with patch("sessions.executor.lock._heartbeat_loop", _loop):
            async with hold_session_lock(NoLock(), thread_id):
                pass

        assert heartbeats == []
        assert await active_holder(thread_id) == "chat-run"


async def test_the_heartbeat_is_cancelled_before_the_slot_is_released():
    thread_id = await amake_job_session()
    order: list[str] = []
    started = asyncio.Event()

    async def _loop(thread_id, holder_id):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("heartbeat cancelled")
            raise

    async def _release(thread_id, holder_id):
        order.append("released")

    with (
        patch("sessions.executor.lock._heartbeat_loop", _loop),
        patch("sessions.executor.lock.SessionLock.release", _release),
    ):
        async with hold_session_lock(Wait(holder_id="run-1", timeout_s=1), thread_id):
            await asyncio.wait_for(started.wait(), timeout=5)

    assert order == ["heartbeat cancelled", "released"]


async def test_a_failed_release_is_logged_not_raised(caplog):
    thread_id = await amake_job_session()
    release = AsyncMock(side_effect=RuntimeError("db down"))

    with patch("sessions.executor.lock.SessionLock.release", release), caplog.at_level("ERROR", logger="daiv.sessions"):
        async with hold_session_lock(Wait(holder_id="run-1", timeout_s=1), thread_id):
            pass

    release.assert_awaited_once_with(thread_id, "run-1")
    assert "failed to release session lock" in caplog.text


@pytest.mark.parametrize(
    ("policy", "active_run_id", "expected"),
    [
        (Wait(holder_id="run-1", timeout_s=1), None, "run-1"),
        (Held(holder_id="chat-run"), "chat-run", "chat-run"),
        (NoLock(), None, None),
    ],
    ids=["wait", "held", "no-lock"],
)
async def test_it_yields_the_holder_id(policy, active_run_id, expected):
    thread_id = await amake_job_session(active_run_id=active_run_id)

    async with hold_session_lock(policy, thread_id) as holder_id:
        assert holder_id == expected


async def test_a_body_that_heartbeats_itself_gets_no_background_heartbeat():
    thread_id = await amake_job_session()
    loops: list[tuple] = []

    async def _loop(*args):
        loops.append(args)

    with patch("sessions.executor.lock._heartbeat_loop", _loop):
        async with hold_session_lock(Wait(holder_id="run-1", timeout_s=1), thread_id, background_heartbeat=False):
            await asyncio.sleep(0)

    assert loops == []
    assert await active_holder(thread_id) is None


class TestStillHeld:
    @pytest.mark.parametrize("verdict", [True, False])
    async def test_it_reports_the_heartbeats_verdict(self, verdict):
        heartbeat = AsyncMock(return_value=verdict)

        with patch("sessions.executor.lock.SessionLock.heartbeat", heartbeat):
            assert await still_held("t-1", "run-1") is verdict

        heartbeat.assert_awaited_once_with("t-1", "run-1")

    async def test_a_failed_heartbeat_is_logged_and_counts_as_held(self, caplog):
        with (
            patch("sessions.executor.lock.SessionLock.heartbeat", AsyncMock(side_effect=RuntimeError("db down"))),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            assert await still_held("t-1", "run-1") is True

        assert "heartbeat failed" in caplog.text


async def _holder_session(*, trigger_type=SessionOrigin.API_JOB, status=RunStatus.RUNNING, **fields):
    thread_id = await amake_job_session(**fields)
    session = await Session.objects.aget(pk=thread_id)
    run = await Run.objects.acreate(
        session=session, trigger_type=trigger_type, repo_id="owner/repo", status=status, started_at=timezone.now()
    )
    return session, run


async def _activity(session):
    return session_activity(session, [r async for r in session.runs.order_by("created_at")])


async def _live_run(session):
    return (await _activity(session)).live


async def test_live_run_is_the_slot_holder_while_in_flight():
    session, run = await _holder_session(active_run_id="42")

    assert await _live_run(session) == LiveRun(id="42", stoppable=False, started_at=run.started_at)


async def test_a_task_backed_run_holding_the_slot_under_its_pk_is_stoppable(create_db_task_result):
    session, run = await _holder_session()
    run.task_result = await sync_to_async(create_db_task_result)(status="RUNNING")
    await run.asave(update_fields=["task_result"])
    await Session.objects.filter(pk=session.pk).aupdate(active_run_id=str(run.pk))
    await session.arefresh_from_db()

    assert await _live_run(session) == LiveRun(id=str(run.pk), stoppable=True, started_at=run.started_at)


async def test_a_webhook_run_holding_the_slot_under_its_own_id_is_not_stoppable(create_db_task_result):
    session, run = await _holder_session(trigger_type=SessionOrigin.ISSUE_WEBHOOK, active_run_id="webhook-1")
    run.task_result = await sync_to_async(create_db_task_result)(status="RUNNING")
    await run.asave(update_fields=["task_result"])

    assert (await _live_run(session)).stoppable is False


async def test_a_background_run_is_stoppable_only_for_the_sessions_owner(create_db_task_result, member_user):
    session, run = await _holder_session(user=member_user)
    run.task_result = await sync_to_async(create_db_task_result)(status="RUNNING")
    await run.asave(update_fields=["task_result"])
    await Session.objects.filter(pk=session.pk).aupdate(active_run_id=str(run.pk))
    await session.arefresh_from_db()
    other = await User.objects.acreate(username="viewer", email="viewer@example.com")
    live = await _live_run(session)

    assert (await sync_to_async(live_run_for)(live, session, member_user)).stoppable is True
    assert (await sync_to_async(live_run_for)(live, session, other)).stoppable is False


async def test_a_chat_only_session_is_stoppable():
    session, _run = await _holder_session(trigger_type=SessionOrigin.CHAT, active_run_id="agui-1")

    assert (await _live_run(session)).stoppable is True


async def test_a_job_queued_behind_a_chat_turn_leaves_the_turn_stoppable():
    session, chat_run = await _holder_session(trigger_type=SessionOrigin.CHAT, active_run_id="agui-1")
    await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id="owner/repo", status=RunStatus.QUEUED
    )

    assert await _live_run(session) == LiveRun(id="agui-1", stoppable=True, started_at=chat_run.started_at)


@pytest.mark.parametrize("stale", [False, True])
async def test_no_live_run_without_a_holder_or_with_a_stale_heartbeat(stale):
    fields = {"active_run_id": "42", "last_active_at": stale_cutoff() - timedelta(minutes=1)} if stale else {}
    session, _run = await _holder_session(status=RunStatus.QUEUED, **fields)

    assert await _live_run(session) is None


async def test_a_webhook_run_waiting_for_the_slot_leaves_the_chat_turn_stoppable():
    session, chat_run = await _holder_session(trigger_type=SessionOrigin.CHAT, active_run_id="agui-1")
    await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.ISSUE_WEBHOOK,
        repo_id="owner/repo",
        status=RunStatus.RUNNING,
        started_at=timezone.now(),
    )

    assert await _live_run(session) == LiveRun(id="agui-1", stoppable=True, started_at=chat_run.started_at)


async def test_a_job_holder_is_the_run_its_holder_id_names():
    session, waiting = await _holder_session(trigger_type=SessionOrigin.MR_WEBHOOK, active_run_id="placeholder")
    holder = await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.API_JOB,
        repo_id="owner/repo",
        status=RunStatus.RUNNING,
        started_at=timezone.now(),
    )
    session.active_run_id = str(holder.pk)

    activity = session_activity(session, [waiting, holder])

    assert activity.holder == holder
    assert activity.live.started_at == holder.started_at


async def test_live_run_does_not_depend_on_the_runs_order():
    session, chat_run = await _holder_session(trigger_type=SessionOrigin.CHAT, active_run_id="agui-1")
    await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id="owner/repo", status=RunStatus.QUEUED
    )

    newest_first = [r async for r in session.runs.order_by("-created_at")]

    assert session_activity(session, newest_first) == await _activity(session)


async def test_a_free_slot_leaves_every_unfinished_job_waiting_even_once_its_task_started():
    session, running = await _holder_session()
    queued = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id="owner/repo", status=RunStatus.QUEUED
    )

    activity = await _activity(session)

    assert activity.waiting == (running, queued)
    assert activity.waiting_since == running.created_at
    assert activity.holder is None
    assert activity.live is None


async def test_an_unfinished_chat_run_never_waits():
    session, _run = await _holder_session(trigger_type=SessionOrigin.CHAT)

    assert (await _activity(session)).waiting == ()


async def test_a_held_slot_leaves_nothing_waiting():
    session, run = await _holder_session(trigger_type=SessionOrigin.API_JOB)
    session.active_run_id = str(run.pk)

    activity = await _activity(session)

    assert activity.waiting == ()
    assert activity.holder == run


async def test_a_stale_session_has_no_activity():
    session, _run = await _holder_session(status=RunStatus.READY, last_active_at=stale_cutoff() - timedelta(minutes=1))

    assert await _activity(session) == SessionActivity()


async def test_a_webhook_holder_beats_an_older_job_still_waiting_for_the_slot():
    session, job = await _holder_session(trigger_type=SessionOrigin.API_JOB, active_run_id="webhook-abc")
    webhook = await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.MR_WEBHOOK,
        repo_id="owner/repo",
        status=RunStatus.RUNNING,
        started_at=timezone.now(),
    )

    activity = await _activity(session)

    assert activity.holder == webhook
    assert job.created_at < webhook.created_at
