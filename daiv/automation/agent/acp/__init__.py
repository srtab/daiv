from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from automation.agent.graph import create_daiv_agent
from codebase.context import LocalRuntimeCtx, create_local_runtime_ctx

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


async def _build_agent(cwd: Path) -> tuple[CompiledStateGraph, LocalRuntimeCtx]:
    """Build a DAIV agent and return both the graph and its context."""
    ctx = create_local_runtime_ctx(cwd)
    agent = await create_daiv_agent(
        ctx=ctx,
        auto_commit_changes=False,
        # Sandbox requires the daiv-sandbox service which isn't available in standalone/ACP mode.
        # Users can override via .daiv.yml if they have a sandbox running.
        sandbox_enabled=False,
    )
    return agent, ctx


def create_acp_server():
    """
    Create an AgentServerACP instance configured with the DAIV agent factory.

    Uses a subclass that:
    - Supports async factory functions (create_daiv_agent is async)
    - Injects LocalRuntimeCtx as the `context` kwarg on agent invocations,
      since the upstream AgentServerACP doesn't pass it to astream()
    """
    from deepagents_acp.server import AgentServerACP

    class DAIVAgentServerACP(AgentServerACP):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._session_ctx: dict[str, LocalRuntimeCtx] = {}
            self._logger = __import__("logging").getLogger("daiv.acp")

        async def list_sessions(self, cursor=None, cwd=None, **kwargs):
            from acp.schema import ListSessionsResponse

            self._logger.info("list_sessions called")
            return ListSessionsResponse(sessions=[])

        async def load_session(self, cwd, session_id, mcp_servers=None, **kwargs):
            self._logger.info("load_session called: session_id=%s", session_id)
            return None

        async def close_session(self, session_id, **kwargs):
            self._logger.info("close_session: session_id=%s", session_id)
            self._session_ctx.pop(session_id, None)
            return None

        async def new_session(self, cwd, mcp_servers=None, **kwargs):
            self._logger.info("new_session called: cwd=%s", cwd)
            return await super().new_session(cwd, mcp_servers or [], **kwargs)

        async def set_session_mode(self, mode_id, session_id, **kwargs):
            self._logger.info("set_session_mode: mode=%s session=%s", mode_id, session_id)
            return await super().set_session_mode(mode_id, session_id, **kwargs)

        async def set_config_option(self, *args, **kwargs):
            self._logger.info("set_config_option: args=%s kwargs=%s", args, kwargs)
            try:
                return await super().set_config_option(*args, **kwargs)
            except Exception:
                self._logger.exception("set_config_option failed")
                raise

        async def prompt(self, prompt, session_id, **kwargs):
            if self._agent is None:
                cwd = self._session_cwds.get(session_id)
                if cwd is not None:
                    self._cwd = cwd

                agent, ctx = await _build_agent(Path(self._cwd) if self._cwd else Path.cwd())
                self._agent = agent
                self._session_ctx[session_id] = ctx

                from langgraph.checkpoint.memory import MemorySaver

                if getattr(self._agent, "checkpointer", None) is None:
                    self._agent.checkpointer = MemorySaver()

            # Wrap the agent to inject context= into astream/ainvoke calls
            ctx = self._session_ctx.get(session_id)
            if ctx is not None:
                self._agent = _ContextInjectingAgent(self._agent, ctx)

            try:
                return await super().prompt(prompt, session_id, **kwargs)
            except Exception:
                import logging

                logging.getLogger("daiv.acp").exception("Error in prompt handler")
                raise
            finally:
                # Unwrap so the real agent is stored for next call
                if isinstance(self._agent, _ContextInjectingAgent):
                    self._agent = self._agent._agent

    # Dummy factory — never actually called since we override prompt()
    return DAIVAgentServerACP(agent=lambda ctx: None)


class _ContextInjectingAgent:
    """Wraps a CompiledStateGraph to inject `context=` into astream/ainvoke calls."""

    def __init__(self, agent: CompiledStateGraph, ctx: LocalRuntimeCtx):
        self._agent = agent
        self._ctx = ctx

    def astream(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("context", self._ctx)
        return self._agent.astream(*args, **kwargs)

    async def aget_state(self, *args: Any, **kwargs: Any):
        return await self._agent.aget_state(*args, **kwargs)

    async def aupdate_state(self, *args: Any, **kwargs: Any):
        return await self._agent.aupdate_state(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)
