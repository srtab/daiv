from __future__ import annotations

import uuid
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import HumanMessage
from sessions.executor.lock import NoLock
from sessions.executor.spec import RunSpec

from automation.agent.agent_settings import RunOverrides
from codebase.base import Scope
from core.site_settings import site_settings
from tests.unit_tests.conftest import agent_settings, site_snapshot, stub_sandbox_spec
from tests.unit_tests.sessions.conftest import watch_recorder

if TYPE_CHECKING:
    from automation.agent.agent_settings import AgentSettings
    from codebase.base import MergeRequest
    from core.site_settings import SiteSnapshot


def resolved_to(*model_names: str, thinking_level: str | None = None) -> AgentSettings:
    """Settings whose agent runs on ``model_names`` with ``thinking_level``, the rest at the defaults."""
    return agent_settings(run=RunOverrides(model_names=model_names, agent_thinking_level=thinking_level))


def make_spec(**overrides) -> RunSpec:
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


@contextmanager
def agent_stack(agent, *, ctx=None, context=None, resolve=None, site: SiteSnapshot | None = None):
    """Stub everything ``execute_run`` builds around ``agent``; the yielded namespace records what it saw.

    ``ctx`` is the ``RuntimeCtx`` the stubbed clone yields (its ``repo.ref`` defaults to ``"main"``), ``context``
    replaces ``set_runtime_ctx`` itself, ``resolve`` replaces ``resolve_agent_settings`` (by default it resolves the
    agent to ``claude-4-7-opus`` at ``medium``), and ``site`` is the snapshot the run takes (the field defaults).
    ``build_spec`` stubs ``build_sandbox_spec``; the spec it returns is the one handed to the clone.
    """
    stack = SimpleNamespace(
        events=[],
        context_kwargs={},
        ctx=ctx
        if ctx is not None
        else MagicMock(repo=SimpleNamespace(ref="main", clone_seconds=1.5), sandbox=None, sandbox_client=None),
        checkpointer=object(),
        armed=[],
        resolve=resolve or MagicMock(return_value=resolved_to("claude-4-7-opus", "fallback", thinking_level="medium")),
        site=site or site_snapshot(),
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
        return {"response": kwargs["response"], "question": kwargs.get("question")}

    class _Watch(watch_recorder(stack.armed)):
        async def aarm_after_run(self, **kwargs):
            stack.events.append("watch armed")
            await super().aarm_after_run(**kwargs)

    with (
        stub_sandbox_spec() as build_spec,
        patch("codebase.context.set_runtime_ctx", context or _set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer", _open_checkpointer),
        patch.object(site_settings, "snapshot", return_value=stack.site) as snapshot,
        patch("automation.agent.agent_settings.resolve_agent_settings", stack.resolve),
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)) as create_agent,
        patch("automation.agent.utils.build_langsmith_config", return_value={"configurable": {}}) as langsmith,
        patch("automation.agent.results.build_agent_result", new=AsyncMock(side_effect=_build_result)) as build_result,
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=dict)),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("sessions.services.apersist_session_ref", new=AsyncMock(side_effect=_persist_ref)) as persist,
        patch("sessions.services.areset_session_ref", new=AsyncMock()) as reset,
        patch("sessions.executor.run.PipelineWatch", _Watch),
    ):
        stack.build_spec = build_spec
        stack.snapshot = snapshot
        stack.create_agent = create_agent
        stack.langsmith = langsmith
        stack.build_result = build_result
        stack.persist = persist
        stack.reset = reset
        yield stack


def publisher_through_workspace(created: list, *, publishes: MergeRequest):
    """A ``GitChangePublisher`` stand-in that pushes through the shell of whatever workspace it is handed."""

    class _Publisher:
        def __init__(self, ctx, workspace, *, thread_id):
            self.workspace = workspace
            created.append(self)

        async def publish(self, *, merge_request: MergeRequest | None, as_draft: bool):
            self.target = (merge_request, as_draft)
            await self.workspace.bash.run_commands(["git push origin HEAD"], fail_fast=True)
            return SimpleNamespace(merge_request=publishes, protected_branch_fallback_source=None)

    return _Publisher
