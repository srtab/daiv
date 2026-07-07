import uuid
from datetime import timedelta

from django.utils import timezone

import pytest
from sessions.locks import SessionLock
from sessions.models import Session, SessionOrigin

pytestmark = pytest.mark.django_db


async def _mk_session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.CHAT, "repo_id": "g/r"}
    defaults.update(kwargs)
    return await Session.objects.acreate(**defaults)


async def test_claim_free_slot():
    session = await _mk_session()
    assert await SessionLock.try_claim(session.thread_id, "run-1") is True
    await session.arefresh_from_db()
    assert session.active_run_id == "run-1"


async def test_claim_busy_slot_fails():
    session = await _mk_session(active_run_id="run-1")
    assert await SessionLock.try_claim(session.thread_id, "run-2") is False


async def test_claim_stale_slot_takes_over():
    stale = timezone.now() - timedelta(minutes=31)
    session = await _mk_session(active_run_id="run-1", last_active_at=stale)
    assert await SessionLock.try_claim(session.thread_id, "run-2") is True
    await session.arefresh_from_db()
    assert session.active_run_id == "run-2"


async def test_release_only_by_holder():
    session = await _mk_session(active_run_id="run-1")
    await SessionLock.release(session.thread_id, "run-2")  # not the holder: no-op
    await session.arefresh_from_db()
    assert session.active_run_id == "run-1"
    await SessionLock.release(session.thread_id, "run-1")
    await session.arefresh_from_db()
    assert session.active_run_id is None


async def test_heartbeat_only_by_holder():
    old = timezone.now() - timedelta(minutes=10)
    session = await _mk_session(active_run_id="run-1", last_active_at=old)
    await SessionLock.heartbeat(session.thread_id, "run-2")  # not the holder: no-op
    await session.arefresh_from_db()
    assert session.last_active_at == old
    await SessionLock.heartbeat(session.thread_id, "run-1")
    await session.arefresh_from_db()
    assert session.last_active_at > old
