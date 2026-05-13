from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, TypedDict

if TYPE_CHECKING:
    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig


# Distinguishes "caller did not supply a snapshot" (fetch one) from "caller supplied
# ``None``" (state read failed upstream — produce a snapshot-less result without
# retrying the fetch that already failed).
NO_SNAPSHOT: Final[Any] = object()


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

    usage: dict[str, Any] | None
    """Token usage and cost summary, or None if not available."""


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
            usage=rv.get("usage") if isinstance(rv.get("usage"), dict) else None,
        )
    return AgentResult(
        response=str(rv) if rv else "",
        code_changes=False,
        merge_request_id=None,
        merge_request_web_url=None,
        usage=None,
    )


async def build_agent_result(
    agent: CompiledAgent,
    config: RunnableConfig,
    *,
    response: str,
    usage: dict[str, Any] | None = None,
    snapshot: Any = NO_SNAPSHOT,
) -> AgentResult:
    """Build a standardized :class:`AgentResult` from the agent's persisted state.

    ``code_changes`` is a PrivateStateAttr, so it's omitted from ainvoke output.
    We read it from the persisted checkpoint instead. Callers that have already
    read the state can pass a pre-fetched ``snapshot`` to avoid a redundant Redis
    round-trip; passing ``None`` explicitly signals that the read already failed
    (so we don't silently retry it here and risk drifting from whatever the
    caller decided based on the same failure).
    """
    if snapshot is NO_SNAPSHOT:
        snapshot = await agent.aget_state(config=config)
    if snapshot is None:
        return AgentResult(
            response=response, code_changes=False, merge_request_id=None, merge_request_web_url=None, usage=usage
        )
    mr = snapshot.values.get("merge_request")
    return AgentResult(
        response=response,
        code_changes=bool(snapshot.values.get("code_changes")),
        merge_request_id=mr.merge_request_id if mr else None,
        merge_request_web_url=mr.web_url if mr else None,
        usage=usage,
    )
