import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sessions.executor.lock import Held, NoLock, Wait, hold_session_lock
from sessions.locks import SessionLock

from tests.unit_tests.sessions.conftest import active_holder, amake_job_session

pytestmark = pytest.mark.django_db(transaction=True)


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

    async def test_it_times_out_when_the_slot_never_frees(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        body = AsyncMock()

        with (
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            pytest.raises(TimeoutError, match="not released within"),
        ):
            async with hold_session_lock(Wait(holder_id="run-1", timeout_s=0.05), thread_id):
                await body()

        body.assert_not_awaited()
        assert await active_holder(thread_id) == "chat-run"


class TestHeld:
    async def test_it_keeps_the_callers_claim_and_frees_it_after(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        try_claim = AsyncMock()

        with patch("sessions.executor.lock.SessionLock.try_claim", try_claim):
            async with hold_session_lock(Held(holder_id="chat-run"), thread_id):
                assert await active_holder(thread_id) == "chat-run"

        try_claim.assert_not_awaited()
        assert await active_holder(thread_id) is None

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
