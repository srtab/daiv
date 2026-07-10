from contextlib import asynccontextmanager, suppress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jobs.tasks import run_job_task


@pytest.mark.django_db
async def test_run_job_task_uses_async_redis_saver_with_thread_id():
    """run_job_task must use the shared open_checkpointer (AsyncRedisSaver) and thread its
    thread_id through to the langgraph config."""
    last_message = MagicMock()
    last_message.content = "ok"
    fake_result = {"messages": [last_message]}

    runtime_ctx = MagicMock()
    runtime_ctx.config.models.agent = MagicMock()

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value=fake_result)

    with (
        patch("jobs.tasks.open_checkpointer") as cp_ctx,
        patch("jobs.tasks.set_runtime_ctx") as rc_ctx,
        patch("jobs.tasks.create_daiv_agent", new=AsyncMock(return_value=agent)) as create_agent_mock,
        patch(
            "jobs.tasks.get_daiv_agent_kwargs",
            return_value={"model_names": ["claude-4-7-opus"], "thinking_level": "medium"},
        ),
        patch("jobs.tasks.build_langsmith_config", return_value={"configurable": {"thread_id": "t-123"}}),
        patch("jobs.tasks.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
        patch("jobs.tasks.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("jobs.tasks.track_usage_metadata"),
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


async def test_run_job_task_threads_env_id_to_set_runtime_ctx():
    """run_job_task must forward sandbox_environment_id to set_runtime_ctx as sandbox_env_id."""
    captured: dict = {}

    @asynccontextmanager
    async def _fake_set_runtime_ctx(*args, **kwargs):
        captured.update(kwargs)
        # Yield a stub RuntimeCtx-ish object enough to navigate the rest of the task.
        yield MagicMock(config=MagicMock(models=MagicMock(agent=object())))

    # We're not setting up enough scaffolding to complete the agent invoke;
    # the assertion below is what matters.
    with (
        patch("jobs.tasks._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("jobs.tasks.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("jobs.tasks.open_checkpointer"),
        patch("jobs.tasks.create_daiv_agent", AsyncMock()),
        patch("jobs.tasks.get_daiv_agent_kwargs", return_value={"model_names": ["m"], "thinking_level": None}),
        patch("jobs.tasks.build_langsmith_config", return_value={}),
        patch("jobs.tasks.track_usage_metadata"),
        patch("jobs.tasks.build_agent_result", AsyncMock(return_value="ok")),
        suppress(Exception),
    ):
        await run_job_task.func(repo_id="r/p", prompt="p", thread_id="t1", sandbox_environment_id="env-uuid")

    assert captured["sandbox_env_id"] == "env-uuid"


@pytest.mark.django_db
async def test_run_job_task_forwards_overrides():
    """When called with explicit overrides, the override pair flows into get_daiv_agent_kwargs."""
    last_message = MagicMock()
    last_message.content = "ok"
    fake_result = {"messages": [last_message]}

    runtime_ctx = MagicMock()
    runtime_ctx.config.models.agent = MagicMock()

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value=fake_result)

    captured_kwargs: dict = {}

    def capture(**kwargs):
        captured_kwargs.update(kwargs)
        return {"model_names": ["captured"], "thinking_level": kwargs.get("agent_thinking_level")}

    with (
        patch("jobs.tasks.open_checkpointer") as cp_ctx,
        patch("jobs.tasks.set_runtime_ctx") as rc_ctx,
        patch("jobs.tasks.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch("jobs.tasks.get_daiv_agent_kwargs", side_effect=capture),
        patch("jobs.tasks.build_langsmith_config", return_value={}),
        patch("jobs.tasks.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
        patch("jobs.tasks.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("jobs.tasks.track_usage_metadata"),
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

    assert captured_kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert captured_kwargs["agent_thinking_level"] == "low"
    assert "use_max" not in captured_kwargs


@pytest.mark.django_db
async def test_run_job_task_persists_resolved_model():
    """The resolved model/thinking are written back onto the Run + Session so the
    session detail view reflects what the run actually executed with, not the empty
    "no override" placeholder. This is the case where the schedule pinned no model.
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
    runtime_ctx = MagicMock()
    runtime_ctx.config.models.agent = MagicMock()

    with (
        patch("jobs.tasks._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("jobs.tasks.open_checkpointer") as cp_ctx,
        patch("jobs.tasks.set_runtime_ctx") as rc_ctx,
        patch("jobs.tasks.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch(
            "jobs.tasks.get_daiv_agent_kwargs",
            return_value={"model_names": ["openrouter:z-ai/glm-5.2", "fallback"], "thinking_level": "xhigh"},
        ),
        patch("jobs.tasks.build_langsmith_config", return_value={}),
        patch("jobs.tasks.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
        patch("jobs.tasks.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("jobs.tasks.track_usage_metadata"),
    ):
        cp_ctx.return_value.__aenter__.return_value = object()
        rc_ctx.return_value.__aenter__.return_value = runtime_ctx

        await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id="t-persist", run_id=str(run.pk))

    await session.arefresh_from_db()
    await run.arefresh_from_db()
    assert session.agent_model == "openrouter:z-ai/glm-5.2"
    assert session.agent_thinking_level == "xhigh"
    assert run.agent_model == "openrouter:z-ai/glm-5.2"
    assert run.agent_thinking_level == "xhigh"


@pytest.mark.django_db
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
        patch("jobs.tasks._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("jobs.tasks.open_checkpointer"),
        patch("jobs.tasks.set_runtime_ctx", _boom),
        patch("jobs.tasks.get_daiv_agent_kwargs", return_value={"model_names": ["m"], "thinking_level": "high"}),
        pytest.raises(RuntimeError, match="git clone failed"),
    ):
        await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id="t-fail", run_id=str(run.pk))

    await session.arefresh_from_db()
    await run.arefresh_from_db()
    assert session.agent_model == ""
    assert run.agent_model == ""
