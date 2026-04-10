from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig


class AgentResult(TypedDict):
    """Standardized result returned by every agent task.

    Stored in DBTaskResult.return_value (JSONField).
    """

    response: str
    """The last agent response text."""

    code_changes: bool
    """Whether the agent published code changes to the repository."""

    merge_request_id: int | None
    """The merge request IID/number, or None if no MR is linked."""

    merge_request_web_url: str | None
    """The full URL to the merge request, or None if no MR is linked."""


def parse_agent_result(rv: dict | str | None) -> AgentResult:
    """Parse a DBTaskResult.return_value into an AgentResult.

    Handles the current dict format and legacy formats (plain str
    or old ``{"code_changes": bool}`` without a "response" key).
    """
    if isinstance(rv, dict):
        return AgentResult(
            response=rv.get("response", ""),
            code_changes=bool(rv.get("code_changes")),
            merge_request_id=rv.get("merge_request_id"),
            merge_request_web_url=rv.get("merge_request_web_url"),
        )
    return AgentResult(
        response=str(rv) if rv else "", code_changes=False, merge_request_id=None, merge_request_web_url=None
    )


async def build_agent_result(agent: CompiledAgent, config: RunnableConfig, *, response: str) -> AgentResult:
    """Build a standardized :class:`AgentResult` from the agent's persisted state.

    ``code_changes`` is a PrivateStateAttr, so it's omitted from ainvoke output.
    We read it from the persisted checkpoint instead.
    """
    snapshot = await agent.aget_state(config=config)
    mr = snapshot.values.get("merge_request")
    return AgentResult(
        response=response,
        code_changes=bool(snapshot.values.get("code_changes")),
        merge_request_id=mr.merge_request_id if mr else None,
        merge_request_web_url=mr.web_url if mr else None,
    )
