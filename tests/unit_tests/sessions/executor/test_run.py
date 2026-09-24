import asyncio
import json
import uuid
from contextlib import asynccontextmanager, contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from redis.exceptions import RedisError
from sessions.executor.lock import NoLock, SessionLockTimeoutError, Wait
from sessions.executor.run import execute_run
from sessions.executor.spec import RunHooks, RunSpec
from sessions.models import Run, RunStatus, Session, SessionOrigin

from automation.agent.validators import AgentConfigurationError
from codebase.base import Scope
from codebase.references import ExternalRef
from tests.unit_tests.sessions.conftest import active_holder, amake_job_session, watch_recorder

AGENT_KWARGS = {"model_names": ["claude-4-7-opus", "fallback"], "thinking_level": "medium"}
MR = {"merge_request_id": 7, "source_branch": "feat/published"}


def _spec(**overrides) -> RunSpec:
    fields = {
        "thread_id": str(uuid.uuid4()),
        "repo_id": "owner/repo",
        "scope": Scope.GLOBAL,
        "input_messages": (HumanMessage(content="hi"),),
        "trigger": "job",
        "lock": NoLock(),
        "ref": "main",
    }
    return RunSpec(**(fields | overrides))


def _agent(*, messages: list | None = None, state: dict | None = None) -> AsyncMock:
    agent = AsyncMock()
    agent.get_name = MagicMock(return_value="DAIV Agent")
    agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="done")] if messages is None else messages})
    agent.aget_state = AsyncMock(return_value=MagicMock(values={} if state is None else state))
    return agent


@contextmanager
def _agent_stack(agent, *, context=None, resolve=None):
    """Stub everything ``execute_run`` builds around ``agent``; the yielded namespace records what it saw."""
    stack = SimpleNamespace(
        events=[],
        context_kwargs={},
        ctx=MagicMock(repo=SimpleNamespace(ref="main")),
        checkpointer=object(),
        armed=[],
        resolve=resolve or MagicMock(return_value=AGENT_KWARGS),
    )

    @asynccontextmanager
    async def _set_runtime_ctx(**kwargs):
        stack.context_kwargs.update(kwargs)
        stack.events.append("context entered")
        try:
            yield stack.ctx
        finally:
            stack.events.append("context exited")

    @asynccontextmanager
    async def _open_checkpointer():
        yield stack.checkpointer

    async def _persist_ref(**_kwargs):
        stack.events.append("ref synced")

    async def _build_result(*_args, **_kwargs):
        stack.events.append("result built")
        return {"response": "done"}

    class _Watch(watch_recorder(stack.armed)):
        async def aarm_after_run(self, **kwargs):
            stack.events.append("watch armed")
            await super().aarm_after_run(**kwargs)

    with (
        patch("codebase.context.set_runtime_ctx", context or _set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer", _open_checkpointer),
        patch("automation.agent.utils.get_daiv_agent_kwargs", stack.resolve),
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)) as create_agent,
        patch("automation.agent.utils.build_langsmith_config", return_value={"configurable": {}}) as langsmith,
        patch("automation.agent.results.build_agent_result", new=AsyncMock(side_effect=_build_result)) as build_result,
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=dict)),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("sessions.services.apersist_session_ref", new=AsyncMock(side_effect=_persist_ref)) as persist,
        patch("sessions.executor.run.PipelineWatch", _Watch),
    ):
        stack.create_agent = create_agent
        stack.langsmith = langsmith
        stack.build_result = build_result
        stack.persist = persist
        yield stack


async def test_it_builds_the_context_and_the_agent_from_the_spec():
    agent = _agent()
    refs = (ExternalRef(key="PROJ-1", provider="jira"),)
    spec = _spec(
        ref="feat/x",
        agent_model="openrouter:z-ai/glm-5.2",
        agent_thinking_level="low",
        sandbox_env_id="env-1",
        acting_user_id=7,
        mcp_overrides={"sentry": "off"},
        references=refs,
        extra_metadata={"ref": "feat/x"},
    )

    with _agent_stack(agent) as stack:
        await execute_run(spec)

    assert stack.context_kwargs == {
        "repo_id": "owner/repo",
        "scope": Scope.GLOBAL,
        "ref": "feat/x",
        "sandbox_env_id": "env-1",
        "acting_user_id": 7,
        "mcp_overrides": {"sentry": "off"},
        "references": refs,
    }
    stack.resolve.assert_called_once_with(
        model_config=stack.ctx.config.models.agent, agent_model="openrouter:z-ai/glm-5.2", agent_thinking_level="low"
    )
    stack.create_agent.assert_awaited_once_with(ctx=stack.ctx, checkpointer=stack.checkpointer, **AGENT_KWARGS)
    stack.langsmith.assert_called_once_with(
        stack.ctx,
        trigger="job",
        model="claude-4-7-opus",
        thinking_level="medium",
        agent_name="DAIV Agent",
        extra_metadata={"ref": "feat/x"},
        configurable={"thread_id": spec.thread_id},
    )
    assert agent.ainvoke.call_args.args == ({"messages": list(spec.input_messages)},)
    assert agent.ainvoke.call_args.kwargs == {"config": stack.langsmith.return_value, "context": stack.ctx}


async def test_it_returns_the_outcome_and_hands_it_to_on_success():
    agent = _agent(state={"merge_request": None})
    on_success = AsyncMock()
    on_failure = AsyncMock()

    with _agent_stack(agent) as stack:
        outcome = await execute_run(_spec(), RunHooks(on_success=on_success, on_failure=on_failure))

    assert outcome.response_text == "done"
    assert outcome.agent_result == {"response": "done"}
    assert outcome.snapshot is agent.aget_state.return_value
    agent.aget_state.assert_awaited_once_with(config=stack.langsmith.return_value)
    stack.build_result.assert_awaited_once_with(
        agent, stack.langsmith.return_value, response="done", usage={}, snapshot=agent.aget_state.return_value
    )
    on_success.assert_awaited_once_with(outcome)
    on_failure.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
async def test_the_steps_run_in_order_inside_the_slot():
    thread_id = await amake_job_session()
    agent = _agent(state={"merge_request": MR, "published": True})
    spec = _spec(thread_id=thread_id, lock=Wait(holder_id="run-1", timeout_s=1), persist_ref=True, arm_watch=True)

    with _agent_stack(agent) as stack:

        async def _invoke(*_args, **_kwargs):
            stack.events.append(f"invoked holding {await active_holder(thread_id)}")
            return {"messages": [MagicMock(content="done")]}

        async def _on_success(_outcome):
            stack.events.append(f"on_success holding {await active_holder(thread_id)}")

        agent.ainvoke = AsyncMock(side_effect=_invoke)
        await execute_run(spec, RunHooks(on_success=_on_success))

    assert stack.events == [
        "context entered",
        "invoked holding run-1",
        "ref synced",
        "watch armed",
        "result built",
        "context exited",
        "on_success holding run-1",
    ]
    assert await active_holder(thread_id) is None


@pytest.mark.django_db(transaction=True)
async def test_a_raising_agent_still_runs_every_finally_step():
    thread_id = await amake_job_session()
    started = asyncio.Event()
    heartbeat_cancelled: list[bool] = []

    async def _heartbeat_loop(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            heartbeat_cancelled.append(True)
            raise

    async def _invoke(*_args, **_kwargs):
        await asyncio.wait_for(started.wait(), timeout=5)
        raise RuntimeError("agent blew up")

    agent = _agent()
    agent.ainvoke = AsyncMock(side_effect=_invoke)
    on_success = AsyncMock()
    spec = _spec(thread_id=thread_id, lock=Wait(holder_id="run-1", timeout_s=1), persist_ref=True, arm_watch=True)

    with _agent_stack(agent) as stack, patch("sessions.executor.lock._heartbeat_loop", _heartbeat_loop):

        async def _on_failure(exc, *, draft_published, snapshot):
            stack.events.append(
                f"on_failure {exc} draft={draft_published} snapshot={snapshot} holding {await active_holder(thread_id)}"
            )

        with pytest.raises(RuntimeError, match="agent blew up"):
            await execute_run(spec, RunHooks(on_success=on_success, on_failure=_on_failure))

    assert stack.events == [
        "context entered",
        "context exited",
        "on_failure agent blew up draft=False snapshot=None holding run-1",
    ]
    assert heartbeat_cancelled == [True]
    assert await active_holder(thread_id) is None
    agent.aget_state.assert_not_awaited()
    stack.persist.assert_not_awaited()
    assert stack.armed == []
    on_success.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
async def test_a_failing_context_exit_still_frees_the_slot():
    thread_id = await amake_job_session()

    @asynccontextmanager
    async def _context(**_kwargs):
        yield MagicMock()
        raise OSError("clone cleanup failed")

    with _agent_stack(_agent(), context=_context), pytest.raises(OSError, match="clone cleanup failed"):
        await execute_run(_spec(thread_id=thread_id, lock=Wait(holder_id="run-1", timeout_s=1)))

    assert await active_holder(thread_id) is None


async def test_an_agent_configuration_error_reaches_on_failure_before_any_agent_is_built():
    error = AgentConfigurationError("no default model configured")
    on_failure = AsyncMock()

    with _agent_stack(_agent(), resolve=MagicMock(side_effect=error)) as stack, pytest.raises(AgentConfigurationError):
        await execute_run(_spec(), RunHooks(on_failure=on_failure))

    stack.create_agent.assert_not_awaited()
    on_failure.assert_awaited_once_with(error, draft_published=False, snapshot=None)


async def test_an_agent_that_returns_no_messages_fails_the_run():
    agent = _agent(messages=[])
    on_failure = AsyncMock()

    with _agent_stack(agent), pytest.raises(ValueError, match="no messages"):
        await execute_run(_spec(), RunHooks(on_failure=on_failure))

    agent.aget_state.assert_not_awaited()
    on_failure.assert_awaited_once()


async def test_the_ref_sync_and_the_watch_run_when_asked():
    spec = _spec(persist_ref=True, arm_watch=True, run_id="run-1", acting_user_id=7)

    with (
        _agent_stack(_agent(state={"merge_request": MR, "published": True})) as stack,
        patch("sessions.executor.run._persist_resolved_agent", new=AsyncMock()),
    ):
        await execute_run(spec)

    stack.persist.assert_awaited_once_with(thread_id=spec.thread_id, current_ref="main", merge_request=MR)
    assert stack.armed == [
        {"repo_id": "owner/repo", "run_id": "run-1", "merge_request": MR, "published": True, "user_id": 7}
    ]


@pytest.mark.parametrize(("persist_ref", "arm_watch"), [(True, False), (False, True), (False, False)])
async def test_the_ref_sync_and_the_watch_each_run_only_when_asked(persist_ref, arm_watch):
    with _agent_stack(_agent(state={"merge_request": MR, "published": True})) as stack:
        await execute_run(_spec(persist_ref=persist_ref, arm_watch=arm_watch))

    assert stack.persist.await_count == int(persist_ref)
    assert len(stack.armed) == int(arm_watch)


async def _job_run() -> tuple[Session, Run]:
    session = await Session.objects.aget(thread_id=await amake_job_session())
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.API_JOB, status=RunStatus.RUNNING, repo_id="owner/repo"
    )
    return session, run


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("error", [None, RuntimeError("agent blew up")], ids=["succeeds", "fails"])
async def test_a_run_records_the_model_it_resolved(error):
    session, run = await _job_run()
    agent = _agent()
    agent.ainvoke.side_effect = error

    with _agent_stack(agent), pytest.raises(RuntimeError, match="agent blew up") if error else nullcontext():
        await execute_run(_spec(thread_id=session.thread_id, run_id=str(run.pk)))

    await session.arefresh_from_db()
    await run.arefresh_from_db()
    assert (session.agent_model, session.agent_thinking_level) == ("claude-4-7-opus", "medium")
    assert (run.agent_model, run.agent_thinking_level) == ("claude-4-7-opus", "medium")


@pytest.mark.django_db(transaction=True)
async def test_a_spec_without_a_run_leaves_the_session_model_alone():
    thread_id = await amake_job_session()

    with _agent_stack(_agent()):
        await execute_run(_spec(thread_id=thread_id))

    assert (await Session.objects.aget(thread_id=thread_id)).agent_model == ""


@pytest.mark.django_db(transaction=True)
async def test_a_db_error_during_model_persist_is_swallowed(caplog):
    session, run = await _job_run()

    with (
        _agent_stack(_agent()),
        patch.object(Run.objects, "filter", side_effect=RuntimeError("db connection failed")),
        caplog.at_level("ERROR", logger="daiv.sessions"),
    ):
        outcome = await execute_run(_spec(thread_id=session.thread_id, run_id=str(run.pk)))

    assert outcome.response_text == "done"
    assert "failed to persist resolved agent model" in caplog.text


async def test_a_failing_on_failure_hook_does_not_mask_the_run_error(caplog):
    agent = _agent()
    agent.ainvoke = AsyncMock(side_effect=RuntimeError("agent blew up"))

    with (
        _agent_stack(agent),
        caplog.at_level("ERROR", logger="daiv.sessions"),
        pytest.raises(RuntimeError, match="agent blew up"),
    ):
        await execute_run(_spec(), RunHooks(on_failure=AsyncMock(side_effect=OSError("platform down"))))

    assert "on_failure hook failed" in caplog.text


async def test_a_failing_on_success_hook_propagates_without_calling_on_failure():
    on_failure = AsyncMock()

    with _agent_stack(_agent()), pytest.raises(OSError, match="platform down"):
        await execute_run(
            _spec(), RunHooks(on_success=AsyncMock(side_effect=OSError("platform down")), on_failure=on_failure)
        )

    on_failure.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
async def test_a_lock_that_never_frees_reaches_on_failure():
    """The trigger hears about a run that gave up waiting, so it can tell its user instead of going quiet."""
    thread_id = await amake_job_session(active_run_id="chat-run")
    on_failure = AsyncMock()

    with (
        _agent_stack(_agent()) as stack,
        patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
        pytest.raises(SessionLockTimeoutError, match="not released within"),
    ):
        await execute_run(
            _spec(thread_id=thread_id, lock=Wait(holder_id="run-1", timeout_s=0.05)), RunHooks(on_failure=on_failure)
        )

    [exc] = on_failure.await_args.args
    assert isinstance(exc, SessionLockTimeoutError)
    assert on_failure.await_args.kwargs == {"draft_published": False, "snapshot": None}
    assert stack.events == []
    stack.create_agent.assert_not_awaited()
    assert await active_holder(thread_id) == "chat-run"


@pytest.mark.parametrize(
    "error",
    [RedisError("connection refused"), OSError("network unreachable"), json.JSONDecodeError("bad", "<doc>", 0)],
    ids=["redis", "os", "json"],
)
async def test_a_failed_checkpoint_read_still_finishes_the_run(error, caplog):
    """The agent already finished, so the hook, the ref sync and the watch still run, without a checkpoint."""
    agent = _agent()
    agent.aget_state = AsyncMock(side_effect=error)
    on_success = AsyncMock()

    with _agent_stack(agent) as stack, caplog.at_level("WARNING", logger="daiv.sessions"):
        outcome = await execute_run(_spec(persist_ref=True, arm_watch=True), RunHooks(on_success=on_success))

    assert outcome.snapshot is None
    on_success.assert_awaited_once_with(outcome)
    stack.persist.assert_awaited_once_with(thread_id=ANY, current_ref="main", merge_request=None)
    assert stack.armed == [
        {"repo_id": "owner/repo", "run_id": None, "merge_request": None, "published": False, "user_id": None}
    ]
    assert stack.build_result.await_args.kwargs["snapshot"] is None
    assert "failed to read agent state" in caplog.text


async def test_a_checkpoint_read_that_fails_for_another_reason_fails_the_run():
    """Only transport and serialization errors degrade; a programming error still surfaces."""
    agent = _agent()
    agent.aget_state = AsyncMock(side_effect=KeyError("checkpoint key missing"))
    on_failure = AsyncMock()

    with _agent_stack(agent), pytest.raises(KeyError):
        await execute_run(_spec(), RunHooks(on_failure=on_failure))

    on_failure.assert_awaited_once()


async def test_the_ref_sync_compares_against_the_ref_the_clone_landed_on():
    spec = _spec(ref=None, persist_ref=True)

    with _agent_stack(_agent(state={"merge_request": MR})) as stack:
        stack.ctx.repo.ref = "master"
        await execute_run(spec)

    stack.persist.assert_awaited_once_with(thread_id=spec.thread_id, current_ref="master", merge_request=MR)
