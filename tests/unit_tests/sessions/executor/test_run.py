import asyncio
import json
import uuid
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from redis.exceptions import RedisError
from redisvl.exceptions import RedisSearchError
from sessions.artifacts import aresolve_active_run
from sessions.executor.lock import Held, NoLock, SessionLockLostError, SessionLockTimeoutError, Wait
from sessions.executor.run import RunStoppedError, execute_run, stream_run
from sessions.executor.spec import RunHooks, RunSpec
from sessions.models import Run, RunStatus, Session, SessionOrigin

from automation.agent.validators import AgentConfigurationError
from codebase.base import Scope
from codebase.references import ExternalRef
from tests.unit_tests.sessions.conftest import active_holder, amake_job_session
from tests.unit_tests.sessions.executor.conftest import AGENT_KWARGS, agent_stack

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

    with agent_stack(agent) as stack:
        await execute_run(spec)

    assert stack.context_kwargs == {
        "repo_id": "owner/repo",
        "scope": Scope.GLOBAL,
        "ref": "feat/x",
        "issue": None,
        "merge_request": None,
        "fallback_ref_on_missing": False,
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

    with agent_stack(agent) as stack:
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

    with agent_stack(agent) as stack:

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

    with agent_stack(agent) as stack, patch("sessions.executor.lock._heartbeat_loop", _heartbeat_loop):

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

    with agent_stack(_agent(), context=_context), pytest.raises(OSError, match="clone cleanup failed"):
        await execute_run(_spec(thread_id=thread_id, lock=Wait(holder_id="run-1", timeout_s=1)))

    assert await active_holder(thread_id) is None


async def test_an_agent_configuration_error_reaches_on_failure_before_any_agent_is_built():
    error = AgentConfigurationError("no default model configured")
    on_failure = AsyncMock()

    with agent_stack(_agent(), resolve=MagicMock(side_effect=error)) as stack, pytest.raises(AgentConfigurationError):
        await execute_run(_spec(), RunHooks(on_failure=on_failure))

    stack.create_agent.assert_not_awaited()
    on_failure.assert_awaited_once_with(error, draft_published=False, snapshot=None)


async def test_an_agent_that_returns_no_messages_fails_the_run():
    agent = _agent(messages=[])
    on_failure = AsyncMock()

    with agent_stack(agent), pytest.raises(ValueError, match="no messages"):
        await execute_run(_spec(), RunHooks(on_failure=on_failure))

    agent.aget_state.assert_not_awaited()
    on_failure.assert_awaited_once()


async def test_the_ref_sync_and_the_watch_run_when_asked():
    spec = _spec(persist_ref=True, arm_watch=True, run_id="run-1", acting_user_id=7)

    with (
        agent_stack(_agent(state={"merge_request": MR, "published": True})) as stack,
        patch("sessions.executor.run._persist_resolved_agent", new=AsyncMock()),
    ):
        await execute_run(spec)

    stack.persist.assert_awaited_once_with(thread_id=spec.thread_id, current_ref="main", merge_request=MR)
    assert stack.armed == [
        {"repo_id": "owner/repo", "run_id": "run-1", "merge_request": MR, "published": True, "user_id": 7}
    ]


@pytest.mark.parametrize(("persist_ref", "arm_watch"), [(True, False), (False, True), (False, False)])
async def test_the_ref_sync_and_the_watch_each_run_only_when_asked(persist_ref, arm_watch):
    with agent_stack(_agent(state={"merge_request": MR, "published": True})) as stack:
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

    with agent_stack(agent), pytest.raises(RuntimeError, match="agent blew up") if error else nullcontext():
        await execute_run(_spec(thread_id=session.thread_id, run_id=str(run.pk)))

    await session.arefresh_from_db()
    await run.arefresh_from_db()
    assert (session.agent_model, session.agent_thinking_level) == ("claude-4-7-opus", "medium")
    assert (run.agent_model, run.agent_thinking_level) == ("claude-4-7-opus", "medium")


@pytest.mark.django_db(transaction=True)
async def test_the_agent_runs_with_the_specs_run_bound_for_artifacts():
    session, run = await _job_run()
    agent = _agent()
    resolved = []

    async def _invoke(*_args, **_kwargs):
        resolved.append(await aresolve_active_run(session.thread_id))
        return {"messages": [MagicMock(content="done")]}

    agent.ainvoke = AsyncMock(side_effect=_invoke)
    with agent_stack(agent):
        await execute_run(_spec(thread_id=session.thread_id, run_id=str(run.pk)))

    assert resolved == [run]
    assert await aresolve_active_run(session.thread_id) is None


@pytest.mark.django_db(transaction=True)
async def test_a_spec_without_a_run_leaves_the_session_model_alone():
    thread_id = await amake_job_session()

    with agent_stack(_agent()):
        await execute_run(_spec(thread_id=thread_id))

    assert (await Session.objects.aget(thread_id=thread_id)).agent_model == ""


@pytest.mark.django_db(transaction=True)
async def test_a_db_error_during_model_persist_is_swallowed(caplog):
    session, run = await _job_run()

    with (
        agent_stack(_agent()),
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
        agent_stack(agent),
        caplog.at_level("ERROR", logger="daiv.sessions"),
        pytest.raises(RuntimeError, match="agent blew up"),
    ):
        await execute_run(_spec(), RunHooks(on_failure=AsyncMock(side_effect=OSError("platform down"))))

    assert "on_failure hook failed" in caplog.text


async def test_a_failing_on_success_hook_propagates_without_calling_on_failure():
    on_failure = AsyncMock()

    with agent_stack(_agent()), pytest.raises(OSError, match="platform down"):
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
        agent_stack(_agent()) as stack,
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


@pytest.mark.django_db(transaction=True)
async def test_a_non_timeout_lock_error_reaches_on_failure_without_entering_the_context():
    thread_id = await amake_job_session()
    on_failure = AsyncMock()

    with (
        agent_stack(_agent()) as stack,
        patch("sessions.executor.lock.SessionLock.try_claim", AsyncMock(side_effect=RuntimeError("db down"))),
        pytest.raises(RuntimeError, match="db down"),
    ):
        await execute_run(
            _spec(thread_id=thread_id, lock=Wait(holder_id="run-1", timeout_s=1)), RunHooks(on_failure=on_failure)
        )

    [exc] = on_failure.await_args.args
    assert isinstance(exc, RuntimeError)
    assert on_failure.await_args.kwargs == {"draft_published": False, "snapshot": None}
    assert stack.events == []
    stack.create_agent.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        RedisError("connection refused"),
        OSError("network unreachable"),
        json.JSONDecodeError("bad", "<doc>", 0),
        RedisSearchError("Error while searching: connection refused"),
    ],
    ids=["redis", "os", "json", "redis-search"],
)
async def test_a_failed_checkpoint_read_still_finishes_the_run(error, caplog):
    """The agent already finished, so the hook, the ref sync and the watch still run, without a checkpoint."""
    agent = _agent()
    agent.aget_state = AsyncMock(side_effect=error)
    on_success = AsyncMock()

    with agent_stack(agent) as stack, caplog.at_level("WARNING", logger="daiv.sessions"):
        outcome = await execute_run(_spec(persist_ref=True, arm_watch=True), RunHooks(on_success=on_success))

    assert outcome.snapshot is None
    on_success.assert_awaited_once_with(outcome)
    stack.persist.assert_awaited_once_with(thread_id=ANY, current_ref="main", merge_request=None)
    assert stack.armed == [
        {"repo_id": "owner/repo", "run_id": None, "merge_request": None, "published": False, "user_id": None}
    ]
    assert stack.build_result.await_args.kwargs["snapshot"] is None
    [record] = caplog.records
    assert (record.levelname, record.exc_info[1]) == ("ERROR", error)
    assert "ref sync and the CI watch" in record.getMessage()


async def test_a_checkpoint_read_that_fails_for_another_reason_fails_the_run():
    """Only transport and serialization errors degrade; a programming error still surfaces."""
    agent = _agent()
    agent.aget_state = AsyncMock(side_effect=KeyError("checkpoint key missing"))
    on_failure = AsyncMock()

    with agent_stack(agent), pytest.raises(KeyError):
        await execute_run(_spec(), RunHooks(on_failure=on_failure))

    on_failure.assert_awaited_once()


async def test_the_ref_sync_compares_against_the_ref_the_clone_landed_on():
    spec = _spec(ref=None, persist_ref=True)

    with agent_stack(_agent(state={"merge_request": MR})) as stack:
        stack.ctx.repo.ref = "master"
        await execute_run(spec)

    stack.persist.assert_awaited_once_with(thread_id=spec.thread_id, current_ref="master", merge_request=MR)


async def test_it_hands_the_webhook_context_to_the_clone():
    issue, merge_request = MagicMock(), MagicMock()

    with agent_stack(_agent()) as stack:
        await execute_run(_spec(issue=issue, merge_request=merge_request, fallback_ref_on_missing=True))

    assert stack.context_kwargs["issue"] is issue
    assert stack.context_kwargs["merge_request"] is merge_request
    assert stack.context_kwargs["fallback_ref_on_missing"] is True


@pytest.mark.parametrize(("use_max", "extra"), [(True, {"use_max": True}), (False, {})], ids=["max", "default"])
async def test_use_max_reaches_model_resolution_only_when_set(use_max, extra):
    with agent_stack(_agent()) as stack:
        await execute_run(_spec(use_max=use_max))

    assert stack.resolve.call_args.kwargs == {
        "model_config": stack.ctx.config.models.agent,
        "agent_model": None,
        "agent_thinking_level": None,
        **extra,
    }


class TestRefFallback:
    """The clone degraded off a vanished branch (its MR merged and deleted): the session is re-pinned to where it
    landed, so the next turn doesn't ask for a branch that is gone. Moved from ``address_issue_task``'s tests."""

    @staticmethod
    async def _run(
        *, ref: str | None, cloned_ref: str, fallback: bool = True, reset_error: Exception | None = None
    ) -> SimpleNamespace:
        agent = _agent()
        spec = _spec(ref=ref, fallback_ref_on_missing=fallback)
        with agent_stack(agent) as stack:
            stack.ctx.repo.ref = cloned_ref
            stack.reset.side_effect = reset_error
            await execute_run(spec)
        return SimpleNamespace(reset=stack.reset, spec=spec, agent=agent)

    async def test_a_vanished_branch_re_pins_the_session(self):
        run = await self._run(ref="fix/10", cloned_ref="master")

        run.reset.assert_awaited_once_with(thread_id=run.spec.thread_id, new_ref="master")

    async def test_a_clone_that_landed_on_the_asked_ref_leaves_the_session_alone(self):
        run = await self._run(ref="fix/10", cloned_ref="fix/10")

        run.reset.assert_not_awaited()

    async def test_a_first_turn_never_pins_the_default_branch_onto_the_session(self):
        """A first turn asks for no ref, so the default branch it lands on is not a working branch."""
        run = await self._run(ref=None, cloned_ref="master")

        run.reset.assert_not_awaited()

    async def test_a_spec_without_fallback_never_re_pins(self):
        run = await self._run(ref="fix/10", cloned_ref="master", fallback=False)

        run.reset.assert_not_awaited()

    async def test_a_failed_re_pin_still_runs_the_turn(self, caplog):
        """The fallback clone already succeeded, so a failed write to a cosmetic pointer must not abort the run."""
        with caplog.at_level("ERROR", logger="daiv.sessions"):
            run = await self._run(ref="fix/10", cloned_ref="master", reset_error=RuntimeError("db down"))

        run.reset.assert_awaited_once()
        run.agent.ainvoke.assert_awaited_once()
        assert "failed to reset session ref" in caplog.text


async def test_an_agent_error_recovers_a_draft_inside_the_context_and_tells_on_failure():
    agent = _agent(state={"merge_request": None})
    agent.ainvoke = AsyncMock(side_effect=RuntimeError("agent blew up"))
    on_failure = AsyncMock()
    spec = _spec(recover_draft=True)
    recovered_state = MagicMock(values={"merge_request": MR})

    with agent_stack(agent) as stack:

        async def _recover(*_args, **_kwargs):
            stack.events.append("draft recovered")
            agent.aget_state.return_value = recovered_state
            return True

        with (
            patch("sessions.executor.run.recover_draft", new=AsyncMock(side_effect=_recover)) as recover,
            pytest.raises(RuntimeError, match="agent blew up"),
        ):
            await execute_run(spec, RunHooks(on_failure=on_failure))

    recover.assert_awaited_once_with(stack.ctx, agent, stack.langsmith.return_value, thread_id=spec.thread_id)
    assert stack.events == ["context entered", "draft recovered", "context exited"]
    agent.aget_state.assert_awaited_once_with(config=stack.langsmith.return_value)
    on_failure.assert_awaited_once_with(agent.ainvoke.side_effect, draft_published=True, snapshot=recovered_state)


async def test_a_setup_error_skips_draft_recovery():
    error = AgentConfigurationError("no default model configured")
    on_failure = AsyncMock()

    with (
        agent_stack(_agent(), resolve=MagicMock(side_effect=error)),
        patch("sessions.executor.run.recover_draft", new=AsyncMock()) as recover,
        pytest.raises(AgentConfigurationError),
    ):
        await execute_run(_spec(recover_draft=True), RunHooks(on_failure=on_failure))

    recover.assert_not_awaited()
    on_failure.assert_awaited_once_with(error, draft_published=False, snapshot=None)


async def test_a_post_recovery_snapshot_read_that_fails_does_not_replace_the_agents_error(caplog):
    agent = _agent()
    agent.ainvoke = AsyncMock(side_effect=RuntimeError("agent blew up"))
    agent.aget_state = AsyncMock(side_effect=KeyError("checkpoint key missing"))
    on_failure = AsyncMock()
    spec = _spec(recover_draft=True)

    with (
        agent_stack(agent),
        patch("sessions.executor.run.recover_draft", new=AsyncMock(return_value=True)),
        caplog.at_level("WARNING", logger="daiv.sessions"),
        pytest.raises(RuntimeError, match="agent blew up"),
    ):
        await execute_run(spec, RunHooks(on_failure=on_failure))

    on_failure.assert_awaited_once_with(agent.ainvoke.side_effect, draft_published=True, snapshot=None)
    assert "failed to read agent state" in caplog.text


async def test_on_context_ready_sees_the_landed_ref_after_the_re_pin_and_before_the_model_is_resolved():
    order: list[str] = []

    async def _ready(ref):
        order.append(f"ready on {ref}")

    with agent_stack(_agent()) as stack:
        stack.ctx.repo.ref = "master"
        stack.reset.side_effect = lambda **_kwargs: order.append("re-pinned")
        stack.resolve.side_effect = lambda **_kwargs: order.append("resolved") or AGENT_KWARGS
        await execute_run(_spec(ref="fix/10", fallback_ref_on_missing=True), RunHooks(on_context_ready=_ready))

    assert order == ["re-pinned", "ready on master", "resolved"]


async def test_a_failing_on_context_ready_fails_the_run_before_any_agent_is_built():
    error = RuntimeError("db down")
    on_failure = AsyncMock()

    with agent_stack(_agent()) as stack, pytest.raises(RuntimeError, match="db down"):
        await execute_run(_spec(), RunHooks(on_context_ready=AsyncMock(side_effect=error), on_failure=on_failure))

    stack.create_agent.assert_not_awaited()
    on_failure.assert_awaited_once_with(error, draft_published=False, snapshot=None)


def _stream(*events, error: Exception | None = None):
    """A ``stream_run`` factory yielding ``events``, then raising ``error``. ``runs`` records the ``AgentRun``s it was
    handed; ``closed`` says whether it was closed before its end."""

    async def _factory(run):
        _factory.runs.append(run)
        try:
            for event in events:
                yield event
        except GeneratorExit:
            _factory.closed = True
            raise
        if error is not None:
            raise error

    _factory.runs = []
    _factory.closed = False
    return _factory


@asynccontextmanager
async def _context_failing_on_close(**_kwargs):
    try:
        yield MagicMock(repo=SimpleNamespace(ref="main"))
    finally:
        raise OSError("clone cleanup failed")


async def _drain(spec, stream, hooks=None, *, should_stop=None) -> list:
    stop = should_stop or AsyncMock(return_value=False)
    return [event async for event in stream_run(spec, stream, hooks, should_stop=stop)]


class TestStreamRun:
    async def test_it_yields_the_streams_events_and_finishes_from_the_checkpoint(self):
        agent = _agent(state={"merge_request": MR, "published": True, "messages": [MagicMock(content="done")]})
        stream = _stream("a", "b")
        on_success = AsyncMock()
        should_stop = AsyncMock(return_value=False)
        spec = _spec(input_messages=(), persist_ref=True, arm_watch=True)

        with agent_stack(agent) as stack:
            events = await _drain(spec, stream, RunHooks(on_success=on_success), should_stop=should_stop)

        assert events == ["a", "b"]
        [run] = stream.runs
        assert (run.ctx, run.agent, run.config) == (stack.ctx, agent, stack.langsmith.return_value)
        assert stack.events == ["context entered", "ref synced", "watch armed", "result built", "context exited"]
        assert stack.armed[0]["published"] is True
        [outcome] = on_success.await_args.args
        assert outcome.response_text == "done"
        should_stop.assert_not_awaited()
        agent.ainvoke.assert_not_awaited()

    async def test_a_failing_stream_fails_the_run_without_the_after_run_steps(self):
        error = RuntimeError("agent blew up")
        agent = _agent()
        on_failure = AsyncMock()

        with agent_stack(agent) as stack, pytest.raises(RuntimeError, match="agent blew up"):
            await _drain(
                _spec(persist_ref=True, arm_watch=True), _stream("a", error=error), RunHooks(on_failure=on_failure)
            )

        on_failure.assert_awaited_once_with(error, draft_published=False, snapshot=None)
        assert stack.events == ["context entered", "context exited"]
        agent.aget_state.assert_not_awaited()
        stack.persist.assert_not_awaited()
        assert stack.armed == []

    async def test_a_failing_stream_recovers_a_draft_when_asked(self):
        error = RuntimeError("agent blew up")
        agent = _agent(state={"merge_request": MR})
        on_failure = AsyncMock()

        with (
            agent_stack(agent) as stack,
            patch("sessions.executor.run.recover_draft", new=AsyncMock(return_value=True)) as recover,
            pytest.raises(RuntimeError, match="agent blew up"),
        ):
            await _drain(_spec(recover_draft=True), _stream(error=error), RunHooks(on_failure=on_failure))

        recover.assert_awaited_once_with(stack.ctx, agent, stack.langsmith.return_value, thread_id=ANY)
        on_failure.assert_awaited_once_with(error, draft_published=True, snapshot=agent.aget_state.return_value)

    @pytest.mark.django_db(transaction=True)
    async def test_a_slot_taken_over_mid_stream_stops_and_closes_the_stream(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        stream = _stream("a", "b")
        should_stop = AsyncMock(return_value=True)
        on_failure = AsyncMock()
        seen: list = []

        with (
            agent_stack(_agent()) as stack,
            patch("sessions.executor.run.STREAM_HEARTBEAT_INTERVAL_S", 0.0),
            patch("sessions.executor.lock.SessionLock.heartbeat", AsyncMock(return_value=False)),
            pytest.raises(SessionLockLostError),
        ):
            spec = _spec(thread_id=thread_id, lock=Held(holder_id="chat-run"), persist_ref=True)
            async for event in stream_run(spec, stream, RunHooks(on_failure=on_failure), should_stop=should_stop):
                seen.append(event)

        assert seen == ["a"]
        assert stream.closed
        should_stop.assert_not_awaited()
        assert isinstance(on_failure.await_args.args[0], SessionLockLostError)
        stack.persist.assert_not_awaited()
        assert await active_holder(thread_id) == "chat-run"

    async def test_a_stop_request_stops_and_closes_the_stream(self):
        stream = _stream("a", "b")
        seen: list = []

        with (
            agent_stack(_agent()) as stack,
            patch("sessions.executor.run.STREAM_HEARTBEAT_INTERVAL_S", 0.0),
            patch("sessions.executor.lock.SessionLock.heartbeat", AsyncMock(return_value=True)),
            pytest.raises(RunStoppedError),
        ):
            spec = _spec(lock=Held(holder_id="chat-run"), persist_ref=True)
            async for event in stream_run(spec, stream, should_stop=AsyncMock(return_value=True)):
                seen.append(event)

        assert seen == ["a"]
        assert stream.closed
        stack.persist.assert_not_awaited()

    async def test_a_stream_without_a_slot_never_heartbeats_but_can_still_stop(self):
        heartbeat = AsyncMock()

        with (
            agent_stack(_agent()),
            patch("sessions.executor.run.STREAM_HEARTBEAT_INTERVAL_S", 0.0),
            patch("sessions.executor.lock.SessionLock.heartbeat", heartbeat),
            pytest.raises(RunStoppedError),
        ):
            await _drain(_spec(lock=NoLock()), _stream("a"), should_stop=AsyncMock(return_value=True))

        heartbeat.assert_not_awaited()

    async def test_a_failed_stream_close_is_logged_not_raised(self, caplog):
        async def _factory(_run):
            try:
                yield "a"
            except GeneratorExit:
                raise RuntimeError("close failed") from None

        with (
            agent_stack(_agent()),
            patch("sessions.executor.run.STREAM_HEARTBEAT_INTERVAL_S", 0.0),
            caplog.at_level("ERROR", logger="daiv.sessions"),
            pytest.raises(RunStoppedError),
        ):
            await _drain(_spec(lock=NoLock()), _factory, should_stop=AsyncMock(return_value=True))

        assert "failed to close the stream" in caplog.text

    async def test_a_stream_heartbeats_between_its_events_not_in_the_background(self):
        loops: list[tuple] = []

        async def _loop(*args):
            loops.append(args)

        async def _suspending(_run):
            await asyncio.sleep(0)
            yield "a"

        with agent_stack(_agent()), patch("sessions.executor.lock._heartbeat_loop", _loop):
            await _drain(_spec(lock=Held(holder_id="chat-run")), _suspending)

        assert loops == []

    async def test_a_consumer_that_stops_reading_closes_the_stream_and_the_context(self):
        stream = _stream("a", "b")
        on_success, on_failure = AsyncMock(), AsyncMock()

        with agent_stack(_agent()) as stack:
            events = stream_run(
                _spec(), stream, RunHooks(on_success=on_success, on_failure=on_failure), should_stop=AsyncMock()
            )
            assert await anext(events) == "a"
            await events.aclose()

        assert stream.closed
        assert stack.events == ["context entered", "context exited"]
        on_success.assert_not_awaited()
        on_failure.assert_not_awaited()

    async def test_a_context_that_fails_to_close_under_an_early_close_is_logged_not_raised(self, caplog):
        on_success, on_failure = AsyncMock(), AsyncMock()

        with agent_stack(_agent(), context=_context_failing_on_close), caplog.at_level("ERROR", logger="daiv.sessions"):
            events = stream_run(
                _spec(),
                _stream("a", "b"),
                RunHooks(on_success=on_success, on_failure=on_failure),
                should_stop=AsyncMock(),
            )
            assert await anext(events) == "a"
            await events.aclose()

        assert "failed to close the run" in caplog.text
        on_success.assert_not_awaited()
        on_failure.assert_not_awaited()

    async def test_a_cancellation_whose_context_fails_to_close_is_still_a_cancellation(self, caplog):
        started = asyncio.Event()

        async def _hanging(_run):
            started.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover

        on_failure = AsyncMock()

        with agent_stack(_agent(), context=_context_failing_on_close), caplog.at_level("ERROR", logger="daiv.sessions"):
            task = asyncio.create_task(_drain(_spec(), _hanging, RunHooks(on_failure=on_failure)))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert "failed to close the run" in caplog.text
        on_failure.assert_not_awaited()

    async def test_a_failed_checkpoint_read_still_finishes_the_stream(self):
        agent = _agent()
        agent.aget_state = AsyncMock(side_effect=RedisError("connection refused"))
        on_success = AsyncMock()

        with agent_stack(agent) as stack:
            await _drain(_spec(persist_ref=True, arm_watch=True), _stream("a"), RunHooks(on_success=on_success))

        [outcome] = on_success.await_args.args
        assert (outcome.snapshot, outcome.response_text) == (None, "")
        stack.persist.assert_awaited_once_with(thread_id=ANY, current_ref="main", merge_request=None)
        assert stack.armed[0]["published"] is False

    @pytest.mark.django_db(transaction=True)
    async def test_a_lock_that_never_frees_reaches_on_failure_before_any_event(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        stream = _stream("a")
        on_failure = AsyncMock()

        with (
            agent_stack(_agent()) as stack,
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            pytest.raises(SessionLockTimeoutError),
        ):
            spec = _spec(thread_id=thread_id, lock=Wait(holder_id="run-1", timeout_s=0.05))
            await _drain(spec, stream, RunHooks(on_failure=on_failure))

        assert isinstance(on_failure.await_args.args[0], SessionLockTimeoutError)
        assert stream.runs == []
        assert stack.events == []
