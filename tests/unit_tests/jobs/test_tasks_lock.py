import uuid
from unittest.mock import patch

import pytest
from jobs.tasks import _acquire_session_lock
from sessions.locks import SessionLock
from sessions.models import Session, SessionOrigin

pytestmark = pytest.mark.django_db


async def _mk_session(**kwargs):
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.API_JOB, "repo_id": "g/r"}
    defaults.update(kwargs)
    return await Session.objects.acreate(**defaults)


async def test_acquire_free_lock_immediately():
    session = await _mk_session()
    assert await _acquire_session_lock(session.thread_id, "run-1") is True


async def test_acquire_waits_then_succeeds_when_released():
    session = await _mk_session(active_run_id="chat-run")

    async def _release_soon(*args, **kwargs):
        await SessionLock.release(session.thread_id, "chat-run")

    with patch("jobs.tasks.LOCK_POLL_INTERVAL_S", 0.01), patch("jobs.tasks.asyncio.sleep", side_effect=_release_soon):
        assert await _acquire_session_lock(session.thread_id, "run-1") is True


async def test_acquire_skips_missing_session():
    # No Session row (legacy null-thread activity): don't block, don't crash.
    assert await _acquire_session_lock(str(uuid.uuid4()), "run-1") is None
