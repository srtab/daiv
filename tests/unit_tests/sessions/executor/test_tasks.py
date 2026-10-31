import asyncio
import uuid
from contextlib import asynccontextmanager, contextmanager, nullcontext, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.db import DatabaseError

import pytest
from asgiref.sync import sync_to_async
from sessions.executor.lock import Claimed, SessionLock, Wait
from sessions.executor.run import RunStoppedError
from sessions.executor.tasks import run_job_task
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.conftest import stub_sandbox_spec
from tests.unit_tests.sessions.conftest import active_holder, amake_job_session, claim_recorder
from tests.unit_tests.sessions.executor.conftest import disk_ctx, resolved_to


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_uses_async_redis_saver_with_thread_id():
    """run_job_task must use the shared open_checkpointer (AsyncRedisSaver) and thread its
    thread_id through to the langgraph config."""
    last_message = MagicMock()
    last_message.content = "ok"
    fake_result = {"messages": [last_message]}

    runtime_ctx = disk_ctx()
    runtime_ctx.config.models.agent = MagicMock()

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value=fake_result)
    agent.aget_state = AsyncMock(return_value=MagicMock(values={}))

    await amake_job_session("t-123")

    with (
        patch("core.checkpointer.open_checkpointer") as cp_ctx,
        patch("automation.agent.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)) as create_agent_mock,
        patch(
            "automation.agent.agent_settings.resolve_agent_settings",
            return_value=resolved_to("claude-4-7-opus", thinking_level="medium"),
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={"configurable": {"thread_id": "t-123"}}),
        patch("sessions.executor.result.build_agent_result", return_value={"response": "ok"}),
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
    ):
        sentinel_checkpointer = object()
        cp_ctx.return_value.__aenter__.return_value = sentinel_checkpointer
        rc_ctx.return_value.__aenter__.return_value = runtime_ctx

        await run_job_task.func(repo_id="owner/repo", prompt="hi", ref="main", thread_id="t-123")

    cp_ctx.assert_called_once()
    call_kwargs = agent.ainvoke.call_args.kwargs
    assert call_kwargs["config"]["configurable"]["thread_id"] == "t-123"

    create_agent_kwargs = create_agent_mock.call_args.kwargs
    assert create_agent_kwargs["checkpointer"] is sentinel_checkpointer


async def test_run_job_task_rejects_missing_thread_id():
    """Chat resume relies on the activity row and the checkpointer sharing the
    same thread_id. A silent UUID fallback would break the resume contract.
    """
    with pytest.raises(ValueError, match="non-empty thread_id"):
        await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id="")


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_builds_the_sandbox_spec_from_its_env_id():
    """run_job_task hands sandbox_environment_id to the executor, which builds the run's spec from it once."""
    captured: dict = {}

    @asynccontextmanager
    async def _fake_set_runtime_ctx(*args, **kwargs):
        captured.update(kwargs)
        # Yield a stub RuntimeCtx-ish object enough to navigate the rest of the task.
        yield disk_ctx(config=MagicMock(models=MagicMock(agent=object())))

    await amake_job_session("t1")

    # We're not setting up enough scaffolding to complete the agent invoke;
    # the assertion below is what matters.
    with (
        stub_sandbox_spec() as build_spec,
        patch("sessions.executor.lock._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("automation.agent.context.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer"),
        patch("automation.agent.graph.create_daiv_agent", AsyncMock()),
        patch("automation.agent.agent_settings.resolve_agent_settings", return_value=resolved_to("m")),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("sessions.executor.result.build_agent_result", return_value="ok"),
        suppress(Exception),
    ):
        await run_job_task.func(repo_id="r/p", prompt="p", thread_id="t1", sandbox_environment_id="env-uuid")

    build_spec.assert_awaited_once_with("env-uuid")
    assert captured["sandbox_spec"] is build_spec.return_value


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_forwards_overrides():
    """When called with explicit overrides, the override pair flows into resolve_agent_settings."""
    last_message = MagicMock()
    last_message.content = "ok"
    fake_result = {"messages": [last_message]}

    runtime_ctx = disk_ctx()
    runtime_ctx.config.models.agent = MagicMock()

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value=fake_result)
    agent.aget_state = AsyncMock(return_value=MagicMock(values={}))

    captured_kwargs: dict = {}
    await amake_job_session("t-123")

    def capture(**kwargs):
        captured_kwargs.update(kwargs)
        return resolved_to("captured", thinking_level=kwargs["run"].agent_thinking_level)

    with (
        patch("core.checkpointer.open_checkpointer") as cp_ctx,
        patch("automation.agent.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch("automation.agent.agent_settings.resolve_agent_settings", side_effect=capture),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("sessions.executor.result.build_agent_result", return_value={"response": "ok"}),
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
    ):
        cp_ctx.return_value.__aenter__.return_value = object()
        rc_ctx.return_value.__aenter__.return_value = runtime_ctx

        await run_job_task.func(
            repo_id="owner/repo",
            prompt="hi",
            ref="main",
            thread_id="t-123",
            agent_model="openrouter:anthropic/claude-haiku-4.5",
            agent_thinking_level="low",
        )

    assert captured_kwargs["run"].agent_model == "openrouter:anthropic/claude-haiku-4.5"
    assert captured_kwargs["run"].agent_thinking_level == "low"
    assert captured_kwargs["run"].use_max is False


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_persists_resolved_model():
    """The resolved model/thinking are written back onto the Run so the session detail view reflects what the run
    executed with, not the empty "no override" placeholder; the Session keeps its empty pin, which chat reuses.
    This is the case where the schedule pinned no model.
    """
    from sessions.models import Run, RunStatus, Session, SessionOrigin

    session = await Session.objects.acreate(thread_id="t-persist", origin=SessionOrigin.SCHEDULE, repo_id="owner/repo")
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.SCHEDULE, status=RunStatus.RUNNING, repo_id="owner/repo"
    )
    assert session.agent_model == ""
    assert run.agent_model == ""

    last_message = MagicMock()
    last_message.content = "ok"
    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [last_message]})
    agent.aget_state = AsyncMock(return_value=MagicMock(values={}))
    runtime_ctx = disk_ctx()
    runtime_ctx.config.models.agent = MagicMock()
    runtime_ctx.repo.clone_seconds = 0.0

    with (
        patch("sessions.executor.lock._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("core.checkpointer.open_checkpointer") as cp_ctx,
        patch("automation.agent.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch(
            "automation.agent.agent_settings.resolve_agent_settings",
            return_value=resolved_to("openrouter:z-ai/glm-5.2", "fallback", thinking_level="xhigh"),
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("sessions.executor.result.build_agent_result", return_value={"response": "ok"}),
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
    ):
        cp_ctx.return_value.__aenter__.return_value = object()
        rc_ctx.return_value.__aenter__.return_value = runtime_ctx

        await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id="t-persist", run_id=str(run.pk))

    await session.arefresh_from_db()
    await run.arefresh_from_db()
    assert (session.agent_model, session.agent_thinking_level) == ("", "")
    assert run.agent_model == "openrouter:z-ai/glm-5.2"
    assert run.agent_thinking_level == "xhigh"


async def _execute_run_call(thread_id: str, **task_kwargs) -> dict:
    captured: dict = {}

    async def _execute_run(spec, hooks=None, *, should_stop=None):
        captured.update(spec=spec, should_stop=should_stop)
        return SimpleNamespace(agent_result={"response": "ok"})

    with patch("sessions.executor.run.execute_run", _execute_run):
        await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, **task_kwargs)
    return captured


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_reads_session_mcp_overrides():
    thread_id = str(uuid.uuid4())
    await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.UI_JOB, repo_id="g/r", mcp_overrides={"a": "off"}
    )

    spec = (await _execute_run_call(thread_id))["spec"]

    assert spec.mcp_overrides == {"a": "off"}


@pytest.mark.django_db(transaction=True)
class TestInboxRun:
    async def _inbox_run(self, *, user=None) -> Run:
        session = await Session.objects.acreate(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="g/r", ref="main", user=user
        )
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id="g/r", status=RunStatus.READY
        )
        await InboxEvent.objects.acreate(
            session=session,
            kind=EventKind.CI_FAILED,
            source_key="pipeline:1",
            payload={"pipeline_id": 1, "sha": "abc", "status": "failed", "web_url": "u", "failed_jobs": ["lint"]},
            wake=True,
            ref="main",
            merge_request_iid=9,
            state=EventState.TAKEN,
            run=run,
        )
        await InboxEvent.objects.acreate(
            session=session,
            kind=EventKind.MR_MERGED,
            source_key="mr-state:3:merged",
            payload={"target_branch": "main", "by_username": "sfr"},
            wake=False,
            merge_request_iid=3,
            state=EventState.TAKEN,
            run=run,
        )
        return run

    async def test_it_runs_on_the_claimed_slot_with_the_rendered_input(self, member_user):
        run = await self._inbox_run(user=member_user)

        spec = (await _execute_run_call(run.session_id, run_id=str(run.pk), inbox=True))["spec"]

        assert spec.lock == Claimed(holder_id=str(run.pk))
        context, request = spec.input_messages
        assert "!3 was merged into main" in context.content
        assert request.content.startswith("CI failed on this merge request. Failed jobs: lint.")

    async def test_it_acts_as_the_runs_user_not_the_owner(self, member_user, admin_user):
        run = await self._inbox_run(user=member_user)
        await Run.objects.filter(pk=run.pk).aupdate(user=admin_user)

        spec = (await _execute_run_call(run.session_id, run_id=str(run.pk), inbox=True))["spec"]

        assert spec.acting_user_id == admin_user.pk

    async def test_the_input_factory_reuses_the_events_read_at_start(self, member_user):
        run = await self._inbox_run(user=member_user)
        spec = (await _execute_run_call(run.session_id, run_id=str(run.pk), inbox=True))["spec"]
        await InboxEvent.objects.filter(run=run).adelete()

        context, _request = await spec.input_factory("advanced")

        assert "Your working copy was fast-forwarded." in context.content

    async def test_a_run_whose_events_were_settled_before_it_started_does_not_run(self, member_user):
        run = await self._inbox_run(user=member_user)
        await InboxEvent.objects.filter(run=run).aupdate(state=EventState.PENDING, run=None)

        with pytest.raises(ValueError, match="holds no event to act on"):
            await _execute_run_call(run.session_id, run_id=str(run.pk), inbox=True)

    async def test_a_run_without_events_keeps_its_prompt_and_waits_for_the_slot(self):
        thread_id = await amake_job_session()
        run = await Run.objects.acreate(session_id=thread_id, trigger_type=SessionOrigin.API_JOB, repo_id="g/r")

        spec = (await _execute_run_call(thread_id, run_id=str(run.pk), user_id=42))["spec"]

        assert isinstance(spec.lock, Wait)
        assert [message.content for message in spec.input_messages] == ["hi"]
        assert spec.acting_user_id == 42


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_leaves_model_empty_when_setup_fails_before_resolution():
    """A run that dies before the model is resolved (e.g. the git clone inside
    ``set_runtime_ctx``) leaves ``agent_model`` empty — the UI falls back to the
    "Auto" pill label rather than a persisted value.
    """
    from sessions.models import Run, RunStatus, Session, SessionOrigin

    session = await Session.objects.acreate(thread_id="t-fail", origin=SessionOrigin.SCHEDULE, repo_id="owner/repo")
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.SCHEDULE, status=RunStatus.RUNNING, repo_id="owner/repo"
    )

    @asynccontextmanager
    async def _boom(*args, **kwargs):
        raise RuntimeError("git clone failed")
        yield  # pragma: no cover — generator body never reached past the raise

    with (
        patch("sessions.executor.lock._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("core.checkpointer.open_checkpointer"),
        patch("automation.agent.context.set_runtime_ctx", _boom),
        patch(
            "automation.agent.agent_settings.resolve_agent_settings",
            return_value=resolved_to("m", thinking_level="high"),
        ),
        pytest.raises(RuntimeError, match="git clone failed"),
    ):
        await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id="t-fail", run_id=str(run.pk))

    await session.arefresh_from_db()
    await run.arefresh_from_db()
    assert session.agent_model == ""
    assert run.agent_model == ""


def _agent_with_mr(merge_request, *, published: bool = False):
    """An agent stub whose run left ``merge_request`` in its persisted checkpoint."""
    last_message = MagicMock()
    last_message.content = "ok"
    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [last_message]})
    agent.aget_state = AsyncMock(
        return_value=MagicMock(values={"merge_request": merge_request, "published": published})
    )
    return agent


@contextmanager
def _job_scaffolding(agent, *, real_lock: bool = False, claimed: list[dict] | None = None):
    """Stub everything around ``agent`` and yield the ``set_runtime_ctx`` mock.

    ``real_lock`` keeps the real session lock; ``claimed`` records the merge request claims instead of making them.
    """
    with (
        nullcontext()
        if real_lock
        else patch("sessions.executor.lock._acquire_session_lock", new=AsyncMock(return_value=None)),
        nullcontext() if claimed is None else patch("sessions.executor.run.aclaim_ownership", claim_recorder(claimed)),
        patch("core.checkpointer.open_checkpointer"),
        patch("automation.agent.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch(
            "automation.agent.agent_settings.resolve_agent_settings",
            return_value=resolved_to("claude-4-7-opus", thinking_level="medium"),
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("sessions.executor.result.build_agent_result", return_value={"response": "ok"}),
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("sessions.executor.run._record_measurements", new=AsyncMock()),
    ):
        rc_ctx.return_value.__aenter__.return_value = disk_ctx(
            config=MagicMock(models=MagicMock(agent=object())), repo=SimpleNamespace(ref="main", head_detached=False)
        )
        yield rc_ctx


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_syncs_the_session_ref_with_the_published_branch():
    """A job run has no event stream, so the branch it published to has to come off the
    persisted checkpoint — otherwise the composer pill and the session list keep showing
    the ref the run started from while the MR pill beside them shows the new one.
    """
    await Session.objects.acreate(thread_id="t-ref-job", origin=SessionOrigin.UI_JOB, repo_id="owner/repo", ref="main")

    with _job_scaffolding(_agent_with_mr({"source_branch": "feat/published"})):
        await run_job_task.func(repo_id="owner/repo", prompt="hi", ref="main", thread_id="t-ref-job")

    refreshed = await Session.objects.aget(thread_id="t-ref-job")
    assert refreshed.ref == "feat/published"


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_leaves_the_session_ref_alone_when_nothing_published():
    await Session.objects.acreate(
        thread_id="t-ref-job-2", origin=SessionOrigin.UI_JOB, repo_id="owner/repo", ref="main"
    )

    with _job_scaffolding(_agent_with_mr(None)):
        await run_job_task.func(repo_id="owner/repo", prompt="hi", ref="main", thread_id="t-ref-job-2")

    refreshed = await Session.objects.aget(thread_id="t-ref-job-2")
    assert refreshed.ref == "main"


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_survives_a_failed_ref_sync():
    """The ref is a cosmetic pointer; a DB hiccup writing it must not fail a run that
    already published its work.
    """
    await Session.objects.acreate(
        thread_id="t-ref-job-3", origin=SessionOrigin.UI_JOB, repo_id="owner/repo", ref="main"
    )

    with (
        _job_scaffolding(_agent_with_mr({"source_branch": "feat/published"})),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        result = await run_job_task.func(repo_id="owner/repo", prompt="hi", ref="main", thread_id="t-ref-job-3")

    assert result == {"response": "ok"}


async def _runtime_ctx_kwargs(thread_id: str, **job_kwargs) -> dict:
    """Run a job just far enough to capture the kwargs it hands ``set_runtime_ctx``."""
    captured: dict = {}

    @asynccontextmanager
    async def _fake_set_runtime_ctx(*args, **kwargs):
        captured.update(kwargs)
        yield disk_ctx(config=MagicMock(models=MagicMock(agent=object())))

    with (
        _job_scaffolding(AsyncMock()),
        patch("automation.agent.context.set_runtime_ctx", _fake_set_runtime_ctx),
        suppress(Exception),
    ):
        await run_job_task.func(repo_id="g/r", prompt="p", thread_id=thread_id, **job_kwargs)

    return captured


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_forwards_ref_and_acting_user():
    """Both are now user-visible on the published MR: ``ref`` becomes its target branch and the
    acting user becomes its assignee."""
    captured = await _runtime_ctx_kwargs(await amake_job_session(), ref="master", user_id=42)

    assert captured.get("ref") == "master"
    assert captured.get("acting_user_id") == 42


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_forwards_session_references():
    """run_job_task must pass Session.external_refs to set_runtime_ctx as ExternalRef objects."""
    from automation.agent.references import ExternalRef

    thread_id = str(uuid.uuid4())
    await Session.objects.acreate(
        thread_id=thread_id,
        origin=SessionOrigin.MCP_JOB,
        repo_id="g/r",
        external_refs=[{"key": "PROJ-1", "provider": "jira", "url": "", "relation": "relates"}],
    )

    captured = await _runtime_ctx_kwargs(thread_id)

    assert captured.get("references") == (ExternalRef(key="PROJ-1", provider="jira"),)


def _raising_agent() -> AsyncMock:
    agent = AsyncMock()
    agent.ainvoke = AsyncMock(side_effect=RuntimeError("agent blew up"))
    return agent


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ask_user_enabled", [True, False])
async def test_run_job_task_forwards_ask_user_enabled(ask_user_enabled):
    captured = await _execute_run_call(await amake_job_session(), ask_user_enabled=ask_user_enabled)

    assert captured["spec"].ask_user_enabled is ask_user_enabled


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fork_branch", [True, False])
async def test_run_job_task_hands_fork_branch_to_the_agent(fork_branch):
    captured = await _execute_run_call(await amake_job_session(), fork_branch=fork_branch)

    assert captured["spec"].agent_options == {"fork_branch": fork_branch}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("run_id", "checks"), [("run-9", True), (None, False)], ids=["run", "no-run"])
async def test_run_job_task_checks_for_a_cancel_only_when_it_has_a_run(run_id, checks):
    captured = await _execute_run_call(await amake_job_session(), run_id=run_id)

    assert (captured["should_stop"] is not None) is checks


@pytest.mark.django_db(transaction=True)
class TestRunJobTaskAfterRunMatrix:
    @pytest.mark.parametrize(("run_id", "holder"), [("run-9", "run-9"), (None, "job-3f2c9a1b")])
    async def test_it_holds_the_session_lock_while_the_agent_runs(self, run_id, holder):
        thread_id = await amake_job_session("3f2c9a1b-0000-4000-8000-000000000000")
        agent = _agent_with_mr(None)
        seen: list[str | None] = []

        async def _invoke(*_args, **_kwargs):
            seen.append(await active_holder(thread_id))
            return {"messages": [MagicMock(content="ok")]}

        agent.ainvoke = AsyncMock(side_effect=_invoke)
        with _job_scaffolding(agent, real_lock=True, claimed=[]):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id=run_id)

        assert seen == [holder]
        assert await active_holder(thread_id) is None

    async def test_it_times_out_when_the_slot_never_frees(self):
        thread_id = await amake_job_session(active_run_id="chat-run")

        with (
            _job_scaffolding(_agent_with_mr(None), real_lock=True) as set_ctx,
            patch("sessions.executor.lock.LOCK_WAIT_TIMEOUT_S", 0.05),
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.try_claim", wraps=SessionLock.try_claim) as try_claim,
            pytest.raises(TimeoutError, match="not released within"),
        ):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id="run-1")

        assert try_claim.await_count >= 2
        set_ctx.assert_not_called()
        assert await active_holder(thread_id) == "chat-run"

    async def test_it_waits_for_a_held_slot_and_runs_once_it_frees(self):
        thread_id = await amake_job_session(active_run_id="chat-run")
        real_try_claim = SessionLock.try_claim
        attempts: list[bool] = []

        async def _try_claim(thread_id, holder_id):
            claimed = await real_try_claim(thread_id, holder_id)
            attempts.append(claimed)
            if not claimed:
                await SessionLock.release(thread_id, "chat-run")
            return claimed

        agent = _agent_with_mr(None)
        seen: list[str | None] = []

        async def _invoke(*_args, **_kwargs):
            seen.append(await active_holder(thread_id))
            return {"messages": [MagicMock(content="ok")]}

        agent.ainvoke = AsyncMock(side_effect=_invoke)
        with (
            _job_scaffolding(agent, real_lock=True, claimed=[]),
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.try_claim", _try_claim),
        ):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id="run-1")

        assert attempts == [False, True]
        assert seen == ["run-1"]

    async def test_a_missing_session_row_fails_the_job_before_it_waits_for_the_slot(self):
        with (
            _job_scaffolding(_agent_with_mr(None), real_lock=True) as set_ctx,
            patch("sessions.executor.lock.SessionLock.try_claim") as try_claim,
            pytest.raises(Session.DoesNotExist),
        ):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=str(uuid.uuid4()))

        try_claim.assert_not_called()
        set_ctx.assert_not_called()

    async def test_it_heartbeats_while_locked_and_cancels_the_heartbeat_after(self):
        thread_id = await amake_job_session()
        started = asyncio.Event()
        heartbeats: list[tuple] = []
        cancelled: list[bool] = []

        async def _loop(*args):
            heartbeats.append(args)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        async def _invoke(*_args, **_kwargs):
            await asyncio.wait_for(started.wait(), timeout=5)
            return {"messages": [MagicMock(content="ok")]}

        agent = _agent_with_mr(None)
        agent.ainvoke = AsyncMock(side_effect=_invoke)
        with (
            _job_scaffolding(agent, real_lock=True, claimed=[]),
            patch("sessions.executor.lock._heartbeat_loop", _loop),
        ):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id="run-1")

        assert heartbeats == [(thread_id, "run-1")]
        assert cancelled == [True]

    async def test_an_agent_error_releases_the_lock_and_skips_every_after_run_step(self):
        """No ref sync, no claim and no draft recovery: the job trigger re-raises as-is."""
        thread_id = await amake_job_session()
        agent = _raising_agent()
        claimed: list[dict] = []
        persist = AsyncMock()

        with (
            _job_scaffolding(agent, real_lock=True, claimed=claimed),
            patch("sessions.executor.run.apersist_session_ref", persist),
            pytest.raises(RuntimeError, match="agent blew up"),
        ):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id="run-1")

        assert await active_holder(thread_id) is None
        agent.aget_state.assert_not_awaited()
        agent.aupdate_state.assert_not_awaited()
        persist.assert_not_awaited()
        assert claimed == []

    async def test_a_failed_run_is_logged_as_job_failed_with_its_error(self, caplog):
        agent = _raising_agent()

        with (
            _job_scaffolding(agent, claimed=[]),
            caplog.at_level("ERROR", logger="daiv.sessions"),
            pytest.raises(RuntimeError, match="agent blew up"),
        ):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=await amake_job_session())

        [record] = [r for r in caplog.records if r.getMessage().startswith("Job failed")]
        assert record.exc_info[1] is agent.ainvoke.side_effect

    async def test_a_run_cancelled_mid_way_stops_and_is_logged_as_cancelled(self, caplog):
        thread_id = await amake_job_session()
        run = await Run.objects.acreate(
            session_id=thread_id, trigger_type=SessionOrigin.API_JOB, repo_id="owner/repo", status=RunStatus.RUNNING
        )
        agent = _agent_with_mr(None)

        async def _invoke(*_args, **_kwargs):
            await sync_to_async(Run.objects.cancel)(run)
            await asyncio.Event().wait()

        agent.ainvoke = AsyncMock(side_effect=_invoke)
        with (
            _job_scaffolding(agent, claimed=[]),
            patch("sessions.executor.run.STOP_POLL_INTERVAL_S", 0.01),
            caplog.at_level("INFO", logger="daiv.sessions"),
            pytest.raises(RunStoppedError),
        ):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id=str(run.pk))

        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("Job cancelled") for m in messages)
        assert not any(m.startswith("Job failed") for m in messages)
        assert await active_holder(thread_id) is None

    async def test_a_cancel_check_that_fails_is_logged_and_the_job_runs_on(self, caplog):
        with (
            _job_scaffolding(_agent_with_mr(None), claimed=[]),
            patch.object(type(Run.objects), "acancel_requested", AsyncMock(side_effect=DatabaseError("db down"))),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            result = await run_job_task.func(
                repo_id="owner/repo", prompt="hi", thread_id=await amake_job_session(), run_id=str(uuid.uuid4())
            )

        assert result is not None
        assert "Could not read whether" in caplog.text

    async def test_it_survives_a_failed_claim(self, caplog):
        claims: list[dict] = []

        with (
            _job_scaffolding(_agent_with_mr(MR, published=True)),
            patch("sessions.executor.run.aclaim_ownership", claim_recorder(claims, error=RuntimeError("db down"))),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            result = await run_job_task.func(
                repo_id="owner/repo", prompt="hi", thread_id=await amake_job_session(), run_id=str(uuid.uuid4())
            )

        assert result == {"response": "ok"}
        assert len(claims) == 1
        assert "failed to claim the merge request" in caplog.text

    async def test_it_survives_a_failed_lock_release(self, caplog):
        thread_id = await amake_job_session()
        release = AsyncMock(side_effect=RuntimeError("db down"))

        with (
            _job_scaffolding(_agent_with_mr(None), real_lock=True, claimed=[]),
            patch("sessions.executor.lock.SessionLock.release", release),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            result = await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id="run-1")

        assert result == {"response": "ok"}
        release.assert_awaited_once_with(thread_id, "run-1")
        assert "failed to release session lock" in caplog.text

    async def test_a_failed_heartbeat_is_logged_and_the_next_one_still_runs(self, caplog):
        thread_id = await amake_job_session()
        beats: list[str] = []
        recovered = asyncio.Event()

        async def _heartbeat(thread_id, holder_id):
            beats.append(holder_id)
            if len(beats) == 1:
                raise RuntimeError("db down")
            recovered.set()
            return True

        async def _invoke(*_args, **_kwargs):
            await asyncio.wait_for(recovered.wait(), timeout=5)
            return {"messages": [MagicMock(content="ok")]}

        agent = _agent_with_mr(None)
        agent.ainvoke = AsyncMock(side_effect=_invoke)
        with (
            _job_scaffolding(agent, real_lock=True, claimed=[]),
            patch("sessions.executor.lock.LOCK_HEARTBEAT_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.heartbeat", _heartbeat),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            result = await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id="run-1")

        assert result == {"response": "ok"}
        assert beats[:2] == ["run-1", "run-1"]
        assert "heartbeat failed" in caplog.text

    async def test_a_lost_slot_is_logged_and_the_job_runs_to_completion(self, caplog):
        """A job whose slot is taken over mid-run logs it, runs its after-run steps, and its release leaves
        the new holder's slot alone."""
        thread_id = await amake_job_session()
        real_heartbeat = SessionLock.heartbeat
        lost = asyncio.Event()

        async def _heartbeat(thread_id, holder_id):
            still_ours = await real_heartbeat(thread_id, holder_id)
            if not still_ours:
                lost.set()
            return still_ours

        async def _invoke(*_args, **_kwargs):
            await Session.objects.filter(thread_id=thread_id).aupdate(active_run_id="chat-run")
            await asyncio.wait_for(lost.wait(), timeout=5)
            return {"messages": [MagicMock(content="ok")]}

        agent = _agent_with_mr({"source_branch": "feat/published"}, published=True)
        agent.ainvoke = AsyncMock(side_effect=_invoke)
        claimed: list[dict] = []
        with (
            _job_scaffolding(agent, real_lock=True, claimed=claimed),
            patch("sessions.executor.lock.LOCK_HEARTBEAT_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.heartbeat", _heartbeat),
            caplog.at_level("WARNING", logger="daiv.sessions"),
        ):
            result = await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id=thread_id, run_id="run-1")

        assert result == {"response": "ok"}
        assert "lost session lock" in caplog.text
        assert (await Session.objects.aget(thread_id=thread_id)).ref == "feat/published"
        assert len(claimed) == 1
        assert await active_holder(thread_id) == "chat-run"

    async def test_it_persists_the_ref_and_claims_the_merge_request_before_releasing_the_slot(self):
        thread_id = await amake_job_session()
        mr = {"source_branch": "feat/published"}
        agent = _agent_with_mr(mr)
        agent.aget_state = AsyncMock(return_value=MagicMock(values={"merge_request": mr, "published": True}))
        calls: list[tuple] = []

        async def _release(thread_id, holder_id):
            calls.append(("release", thread_id, holder_id))

        async def _persist(*, thread_id, current_ref, merge_request, published):
            calls.append(("persist", current_ref, merge_request))

        record = claim_recorder([])

        async def _claim(**kwargs):
            calls.append(("claim", kwargs["merge_request"]))
            await record(**kwargs)

        with (
            _job_scaffolding(agent, real_lock=True),
            patch("sessions.executor.lock.SessionLock.release", _release),
            patch("sessions.executor.run.apersist_session_ref", _persist),
            patch("sessions.executor.run.aclaim_ownership", _claim),
        ):
            await run_job_task.func(repo_id="owner/repo", prompt="hi", ref="main", thread_id=thread_id, run_id="run-1")

        assert calls == [("persist", "main", mr), ("claim", mr), ("release", thread_id, "run-1")]


MR = {"merge_request_id": 7, "source_branch": "daiv/branch"}


async def _drive(state_values: dict, *, run_id: str | None = None, user_id: int | None = None) -> list[dict]:
    """Run the task over a canned final checkpoint and return the merge request claims."""
    claimed: list[dict] = []

    last_message = MagicMock()
    last_message.content = "ok"

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [last_message]})
    agent.aget_state = AsyncMock(return_value=SimpleNamespace(values=state_values))

    runtime_ctx = disk_ctx()
    runtime_ctx.config.models.agent = MagicMock()
    thread_id = await amake_job_session()

    with (
        patch("sessions.executor.lock._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("core.checkpointer.open_checkpointer") as cp_ctx,
        patch("automation.agent.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch(
            "automation.agent.agent_settings.resolve_agent_settings",
            return_value=resolved_to("m", thinking_level="medium"),
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("sessions.executor.result.build_agent_result", return_value={"response": "ok"}),
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("sessions.executor.run.aclaim_ownership", claim_recorder(claimed)),
        patch("sessions.executor.run._record_measurements", new=AsyncMock()),
    ):
        cp_ctx.return_value.__aenter__.return_value = object()
        rc_ctx.return_value.__aenter__.return_value = runtime_ctx

        await run_job_task.func(
            repo_id="group/repo", prompt="p", ref="main", thread_id=thread_id, run_id=run_id, user_id=user_id
        )

    return claimed


@pytest.mark.django_db(transaction=True)
async def test_it_claims_off_the_published_flag():
    """The claim follows the publisher's own verdict, ``published``, read from the checkpoint the task already fetches
    for ``apersist_session_ref``."""
    claimed = await _drive({"merge_request": MR, "published": True}, run_id="run-1", user_id=9)

    assert claimed == [{"run_id": "run-1", "merge_request": MR, "pushed_sha": ""}]


@pytest.mark.django_db(transaction=True)
async def test_a_clean_tree_on_its_mr_claims_nothing():
    """``code_changes`` is true here, ``published`` is not: a run that pushed nothing moves no ownership and records
    no DAIV commit."""
    claimed = await _drive({"merge_request": MR, "code_changes": True, "published": False}, run_id="run-1")

    assert claimed == []
