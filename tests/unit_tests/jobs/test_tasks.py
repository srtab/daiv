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
        patch("jobs.tasks.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch(
            "jobs.tasks.get_daiv_agent_kwargs",
            return_value={"model_names": ["claude-4-7-opus"], "thinking_level": "medium"},
        ),
        patch("jobs.tasks.build_langsmith_config", return_value={"configurable": {"thread_id": "t-123"}}),
        patch("jobs.tasks.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
        patch("jobs.tasks.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("jobs.tasks.track_usage_metadata"),
    ):
        cp_ctx.return_value.__aenter__.return_value = object()
        rc_ctx.return_value.__aenter__.return_value = runtime_ctx

        await run_job_task.func(repo_id="owner/repo", prompt="hi", ref="main", use_max=False, thread_id="t-123")

    cp_ctx.assert_called_once()
    call_kwargs = agent.ainvoke.call_args.kwargs
    assert call_kwargs["config"]["configurable"]["thread_id"] == "t-123"
