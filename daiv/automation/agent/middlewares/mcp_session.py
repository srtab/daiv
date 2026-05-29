"""Persist MCP session ids in graph state across chat turns.

The actual session opening is owned by ``MCPToolkit.aopen`` at the agent-run
scope; this middleware exists only to push the resulting ids dict into state so
the LangGraph checkpointer carries them to the next turn, where the chat
streaming wrapper reads them back and asks ``aopen`` to resume.

Without this, the dict captured during ``aopen`` would die with the request and
each turn would still get a fresh browser context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState

if TYPE_CHECKING:
    from typing import Any


class MCPSessionState(AgentState):
    """State extension carrying MCP session ids keyed by server name."""

    mcp_session_ids: NotRequired[dict[str, str]]


class MCPSessionStateMiddleware(AgentMiddleware):
    """Writes the current MCP session ids dict into state on each turn.

    The dict is shared by reference with ``MCPToolkit.aopen`` — it's mutated
    in-place when a stale id is recovered, so by the time ``before_agent``
    fires the dict already reflects what actually got connected this turn.
    """

    state_schema = MCPSessionState

    def __init__(self, session_ids: dict[str, str]) -> None:
        self._session_ids = session_ids

    async def abefore_agent(self, state: MCPSessionState, runtime: Any) -> dict[str, Any] | None:  # noqa: ARG002
        if not self._session_ids:
            return None
        return {"mcp_session_ids": dict(self._session_ids)}
