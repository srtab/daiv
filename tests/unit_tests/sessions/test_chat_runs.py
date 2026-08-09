import uuid
from decimal import Decimal

import pytest
from sessions.models import RunStatus, Session, SessionOrigin

from chat.api.streaming import finalize_chat_run, start_chat_run

# ``transaction=True``: these are async DB tests. Async writes commit and escape the
# plain-``django_db`` savepoint rollback (a known footgun in this project's in-memory
# SQLite), so the created Run/Session rows would leak into later tests that make global
# ``Run.objects.count()`` assertions (e.g. ``test_data_migration``). The transactional
# flush after each test cleans them up.
pytestmark = pytest.mark.django_db(transaction=True)


async def _mk_user(django_user_model):
    """Async DB rows escape plain-``django_db`` rollback in this project's in-memory
    SQLite, so give each user a unique username/email to avoid cross-test collisions.
    """
    tag = uuid.uuid4().hex[:8]
    return await django_user_model.objects.acreate_user(
        username=f"u-{tag}",
        email=f"u-{tag}@x.io",
        password="x",  # noqa: S106
    )


async def _mk_chat_session(user) -> Session:
    return await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="g/r", user=user
    )


async def test_start_chat_run_creates_running_run(django_user_model):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hello", repo_id="g/r", ref="main")
    assert run.trigger_type == SessionOrigin.CHAT
    assert run.status == RunStatus.RUNNING
    assert run.started_at is not None
    assert run.task_result_id is None


async def test_finalize_chat_run_success_records_usage(django_user_model):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": "0.01", "by_model": {}}
    await finalize_chat_run(run.pk, success=True, usage=usage, response_text="done")
    await run.arefresh_from_db()
    assert run.status == RunStatus.SUCCESSFUL
    assert run.finished_at is not None
    assert run.total_tokens == 15
    assert run.cost_usd == Decimal("0.01")
    assert run.result_summary == "done"


async def test_finalize_chat_run_failure(django_user_model):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    await finalize_chat_run(run.pk, success=False, usage=None, response_text="")
    await run.arefresh_from_db()
    assert run.status == RunStatus.FAILED
