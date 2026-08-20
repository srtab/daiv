from contextlib import asynccontextmanager, contextmanager, suppress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jobs.tasks import run_job_task
from sessions.models import Session, SessionOrigin


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
        patch("core.checkpointer.open_checkpointer") as cp_ctx,
        patch("codebase.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)) as create_agent_mock,
        patch(
            "automation.agent.utils.get_daiv_agent_kwargs",
            return_value={"model_names": ["claude-4-7-opus"], "thinking_level": "medium"},
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={"configurable": {"thread_id": "t-123"}}),
        patch("automation.agent.results.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
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


@pytest.mark.django_db
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
        patch("codebase.context.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer"),
        patch("automation.agent.graph.create_daiv_agent", AsyncMock()),
        patch(
            "automation.agent.utils.get_daiv_agent_kwargs", return_value={"model_names": ["m"], "thinking_level": None}
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("automation.agent.results.build_agent_result", AsyncMock(return_value="ok")),
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
        patch("core.checkpointer.open_checkpointer") as cp_ctx,
        patch("codebase.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch("automation.agent.utils.get_daiv_agent_kwargs", side_effect=capture),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("automation.agent.results.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
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

    assert captured_kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert captured_kwargs["agent_thinking_level"] == "low"
    assert "use_max" not in captured_kwargs


@pytest.mark.django_db(transaction=True)
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
        patch("core.checkpointer.open_checkpointer") as cp_ctx,
        patch("codebase.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch(
            "automation.agent.utils.get_daiv_agent_kwargs",
            return_value={"model_names": ["openrouter:z-ai/glm-5.2", "fallback"], "thinking_level": "xhigh"},
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("automation.agent.results.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
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


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_reads_session_mcp_overrides():
    """run_job_task must pass Session.mcp_overrides to set_runtime_ctx; missing row → {}."""
    import uuid
    from contextlib import asynccontextmanager, suppress

    from sessions.models import Session, SessionOrigin

    thread_id = str(uuid.uuid4())
    await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.UI_JOB, repo_id="g/r", mcp_overrides={"a": "off"}
    )

    captured: dict = {}

    @asynccontextmanager
    async def _fake_set_runtime_ctx(*args, **kwargs):
        captured.update(kwargs)
        yield MagicMock(config=MagicMock(models=MagicMock(agent=object())))

    with (
        patch("jobs.tasks._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("codebase.context.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer"),
        patch("automation.agent.graph.create_daiv_agent", AsyncMock()),
        patch(
            "automation.agent.utils.get_daiv_agent_kwargs", return_value={"model_names": ["m"], "thinking_level": None}
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("automation.agent.results.build_agent_result", AsyncMock(return_value="ok")),
        suppress(Exception),
    ):
        await run_job_task.func(repo_id="g/r", prompt="p", thread_id=thread_id)

    assert captured.get("mcp_overrides") == {"a": "off"}


@pytest.mark.django_db(transaction=True)
async def test_run_job_task_mcp_overrides_defaults_to_empty_when_no_session():
    """When there is no Session row, mcp_overrides forwarded to set_runtime_ctx is {}."""
    import uuid
    from contextlib import asynccontextmanager, suppress

    captured: dict = {}

    @asynccontextmanager
    async def _fake_set_runtime_ctx(*args, **kwargs):
        captured.update(kwargs)
        yield MagicMock(config=MagicMock(models=MagicMock(agent=object())))

    with (
        patch("jobs.tasks._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("codebase.context.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer"),
        patch("automation.agent.graph.create_daiv_agent", AsyncMock()),
        patch(
            "automation.agent.utils.get_daiv_agent_kwargs", return_value={"model_names": ["m"], "thinking_level": None}
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("automation.agent.results.build_agent_result", AsyncMock(return_value="ok")),
        suppress(Exception),
    ):
        await run_job_task.func(repo_id="g/r", prompt="p", thread_id=str(uuid.uuid4()))

    assert captured.get("mcp_overrides") == {}


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
        patch("jobs.tasks._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("core.checkpointer.open_checkpointer"),
        patch("codebase.context.set_runtime_ctx", _boom),
        patch(
            "automation.agent.utils.get_daiv_agent_kwargs",
            return_value={"model_names": ["m"], "thinking_level": "high"},
        ),
        pytest.raises(RuntimeError, match="git clone failed"),
    ):
        await run_job_task.func(repo_id="owner/repo", prompt="hi", thread_id="t-fail", run_id=str(run.pk))

    await session.arefresh_from_db()
    await run.arefresh_from_db()
    assert session.agent_model == ""
    assert run.agent_model == ""


def _agent_with_mr(merge_request):
    """An agent stub whose run left ``merge_request`` in its persisted checkpoint."""
    last_message = MagicMock()
    last_message.content = "ok"
    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [last_message]})
    agent.aget_state = AsyncMock(return_value=MagicMock(values={"merge_request": merge_request}))
    return agent


@contextmanager
def _job_scaffolding(agent):
    with (
        patch("jobs.tasks._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("core.checkpointer.open_checkpointer"),
        patch("codebase.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch(
            "automation.agent.utils.get_daiv_agent_kwargs",
            return_value={"model_names": ["claude-4-7-opus"], "thinking_level": "medium"},
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("automation.agent.results.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
    ):
        rc_ctx.return_value.__aenter__.return_value = MagicMock(config=MagicMock(models=MagicMock(agent=object())))
        yield


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
        patch("sessions.services.apersist_session_ref", new=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        result = await run_job_task.func(repo_id="owner/repo", prompt="hi", ref="main", thread_id="t-ref-job-3")

    assert result == {"response": "ok"}
