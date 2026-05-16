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

        await run_job_task.func(repo_id="owner/repo", prompt="hi", ref="main", use_max=False, thread_id="t-123")

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
