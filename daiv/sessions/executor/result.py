from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from automation.agent.git_state import checkpointed_merge_request

if TYPE_CHECKING:
    from langgraph.types import StateSnapshot


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

    question: dict[str, Any] | None
    """The question the run ended on (``{"questions": [...]}``), or None when it ended without one."""


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
            question=rv.get("question") if isinstance(rv.get("question"), dict) else None,
        )
    return AgentResult(
        response=str(rv) if rv else "",
        code_changes=False,
        merge_request_id=None,
        merge_request_web_url=None,
        usage=None,
        question=None,
    )


def build_agent_result(
    *,
    response: str,
    snapshot: StateSnapshot | None,
    usage: dict[str, Any] | None = None,
    question: dict[str, Any] | None = None,
) -> AgentResult:
    """Build a standardized :class:`AgentResult` from the agent's persisted state.

    ``code_changes`` is a PrivateStateAttr, so it's omitted from ainvoke output and read from the checkpoint
    ``snapshot`` instead; ``None`` means that read failed, which yields a result without code changes or MR.
    """
    if snapshot is None:
        return AgentResult(
            response=response,
            code_changes=False,
            merge_request_id=None,
            merge_request_web_url=None,
            usage=usage,
            question=question,
        )
    mr = checkpointed_merge_request(snapshot.values, strict=False)
    return AgentResult(
        response=response,
        code_changes=bool(snapshot.values.get("code_changes")),
        merge_request_id=mr.merge_request_id if mr else None,
        merge_request_web_url=mr.web_url if mr else None,
        usage=usage,
        question=question,
    )
