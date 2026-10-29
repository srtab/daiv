import uuid
from decimal import Decimal

import pytest
from sessions.executor.result import parse_agent_result
from sessions.models import Run, RunStatus, Session, SessionOrigin

from automation.agent.events import publish_result_payload
from chat.api.streaming import finalize_chat_run, start_chat_run
from codebase.base import MergeRequestDiffStats

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
    await finalize_chat_run(run, success=True, usage=usage, response_text="done")
    await run.arefresh_from_db()
    assert run.status == RunStatus.SUCCESSFUL
    assert run.finished_at is not None
    assert run.total_tokens == 15
    assert run.cost_usd == Decimal("0.01")
    assert run.result_summary == "done"


async def test_finalize_chat_run_records_the_merge_request(django_user_model):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    await finalize_chat_run(
        run,
        success=True,
        usage=None,
        response_text="done",
        agent_result=parse_agent_result({"merge_request_id": 7, "merge_request_web_url": "https://git.example/mr/7"}),
    )
    await run.arefresh_from_db()
    assert (run.merge_request_iid, run.merge_request_web_url) == (7, "https://git.example/mr/7")


async def test_finalize_chat_run_records_code_changes(django_user_model):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    await finalize_chat_run(
        run, success=True, usage=None, response_text="done", agent_result=parse_agent_result({"code_changes": True})
    )
    await run.arefresh_from_db()
    assert run.code_changes is True


async def test_finalize_chat_run_failure(django_user_model):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    await finalize_chat_run(run, success=False, usage=None, response_text="")
    await run.arefresh_from_db()
    assert run.status == RunStatus.FAILED


async def test_finalize_records_a_question_turn_as_waiting_input(django_user_model):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    question = {"questions": [{"header": "DB", "question": "Which?", "options": [], "multi_select": False}]}
    await finalize_chat_run(run, success=True, usage=None, response_text="**DB**: Which?", question=question)
    await run.arefresh_from_db()
    assert run.status == RunStatus.WAITING_INPUT
    assert run.result_summary == "**DB**: Which?"
    assert run.question == question


async def test_a_failed_turn_is_failed_even_if_it_was_waiting(django_user_model):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    await finalize_chat_run(run, success=False, usage=None, response_text="", question={"questions": []})
    await run.arefresh_from_db()
    assert run.status == RunStatus.FAILED
    assert run.question is None


async def test_finalize_chat_run_leaves_a_reaped_run_as_the_reaper_left_it(django_user_model):
    """A finish never overwrites another: once ``sync_stuck_runs`` failed a run whose heartbeat went stale, a
    stream that finalizes afterwards does not flip it back."""
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, error_message="reaped")

    await finalize_chat_run(run, success=True, usage=None, response_text="done")

    await run.arefresh_from_db()
    assert (run.status, run.error_message, run.result_summary) == (RunStatus.FAILED, "reaped", "")


async def test_finalize_chat_run_keeps_the_usage_of_a_reaped_run(django_user_model, caplog):
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, error_message="reaped")

    await finalize_chat_run(
        run, success=True, usage={"input_tokens": 10, "total_tokens": 12, "cost_usd": "0.5"}, response_text="done"
    )

    await run.arefresh_from_db()
    assert (run.status, run.result_summary) == (RunStatus.FAILED, "")
    assert (run.input_tokens, run.total_tokens, run.cost_usd) == (10, 12, Decimal("0.5"))
    assert f"run={run.pk} finished before its stream did" in caplog.text


async def _finalized_run(django_user_model, **finalize_kwargs) -> Run:
    user = await _mk_user(django_user_model)
    session = await _mk_chat_session(user)
    run = await start_chat_run(session_id=session.thread_id, user_id=user.pk, prompt="hi", repo_id="g/r", ref="main")
    await finalize_chat_run(run, usage=None, response_text="", **finalize_kwargs)
    await run.arefresh_from_db()
    return run


async def test_a_successful_turn_records_its_publish_result(django_user_model):
    payload = publish_result_payload(status="pushed", diff_stats=MergeRequestDiffStats(lines_added=4, files_changed=1))
    run = await _finalized_run(django_user_model, success=True, agent_result=parse_agent_result({"publish": payload}))
    assert (run.publish_status, run.lines_added, run.files_changed, run.lines_removed) == ("pushed", 4, 1, None)
