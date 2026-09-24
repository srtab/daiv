from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests.unit_tests.sessions.conftest import watch_recorder

AGENT_KWARGS = {"model_names": ["claude-4-7-opus", "fallback"], "thinking_level": "medium"}


@contextmanager
def agent_stack(agent, *, ctx=None, context=None, resolve=None):
    """Stub everything ``execute_run`` builds around ``agent``; the yielded namespace records what it saw.

    ``ctx`` is the ``RuntimeCtx`` the stubbed clone yields (its ``repo.ref`` defaults to ``"main"``), ``context``
    replaces ``set_runtime_ctx`` itself, and ``resolve`` replaces ``get_daiv_agent_kwargs``.
    """
    stack = SimpleNamespace(
        events=[],
        context_kwargs={},
        ctx=ctx if ctx is not None else MagicMock(repo=SimpleNamespace(ref="main")),
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

    async def _build_result(*_args, **kwargs):
        stack.events.append("result built")
        return {"response": kwargs["response"]}

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
        patch("sessions.services.areset_session_ref", new=AsyncMock()) as reset,
        patch("sessions.executor.run.PipelineWatch", _Watch),
    ):
        stack.create_agent = create_agent
        stack.langsmith = langsmith
        stack.build_result = build_result
        stack.persist = persist
        stack.reset = reset
        yield stack
