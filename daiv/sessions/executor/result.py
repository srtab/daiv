from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypedDict

from automation.agent.git_state import checkpointed_merge_request, checkpointed_repo_publishes

logger = logging.getLogger("daiv.sessions")

if TYPE_CHECKING:
    from langgraph.types import StateSnapshot


class AgentResult(TypedDict):
    """Standardized result returned by every agent task.

    Stored in DBTaskResult.return_value (JSONField).
    """

    response: str
    """The last agent response text."""

    code_changes: bool
    """Whether the agent published code changes to any of the run's repositories."""

    merge_request_id: int | None
    """The merge request IID/number, or None if no MR is linked."""

    merge_request_web_url: str | None
    """The full URL to the merge request, or None if no MR is linked."""

    merge_requests: list[dict[str, Any]]
    """Every MR the run opened, main repository first: ``{"repo_id", "merge_request_id", "web_url"}``."""

    publish_errors: list[dict[str, str]]
    """Repositories whose publish failed: ``{"repo_id", "error"}``."""

    usage: dict[str, Any] | None
    """Token usage and cost summary, or None if not available."""

    question: dict[str, Any] | None
    """The question the run ended on (``{"questions": [...]}``), or None when it ended without one."""


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


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
            merge_requests=_dict_items(rv.get("merge_requests")),
            publish_errors=_dict_items(rv.get("publish_errors")),
            usage=rv.get("usage") if isinstance(rv.get("usage"), dict) else None,
            question=rv.get("question") if isinstance(rv.get("question"), dict) else None,
        )
    return AgentResult(
        response=str(rv) if rv else "",
        code_changes=False,
        merge_request_id=None,
        merge_request_web_url=None,
        merge_requests=[],
        publish_errors=[],
        usage=None,
        question=None,
    )


def build_agent_result(
    *,
    response: str,
    snapshot: StateSnapshot | None,
    usage: dict[str, Any] | None = None,
    question: dict[str, Any] | None = None,
    main_repo_id: str,
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
            merge_requests=[],
            publish_errors=[],
            usage=usage,
            question=question,
        )
    mr = checkpointed_merge_request(snapshot.values, strict=False)
    publishes = checkpointed_repo_publishes(snapshot.values)
    owned = [(main_repo_id, mr)] if mr else []
    owned += [(rp.repo_id, rp.merge_request) for rp in publishes if rp.merge_request]
    merge_requests: list[dict[str, Any]] = [
        {"repo_id": repo_id, "merge_request_id": m.merge_request_id, "web_url": m.web_url} for repo_id, m in owned
    ]
    errors = [(main_repo_id, snapshot.values.get("main_publish_error")), *((rp.repo_id, rp.error) for rp in publishes)]
    publish_errors = [{"repo_id": repo_id, "error": error} for repo_id, error in errors if error]
    return AgentResult(
        response=response,
        code_changes=bool(snapshot.values.get("code_changes"))
        or any(not rp.error and (rp.published or rp.merge_request is not None) for rp in publishes),
        merge_request_id=mr.merge_request_id if mr else None,
        merge_request_web_url=mr.web_url if mr else None,
        merge_requests=merge_requests,
        publish_errors=publish_errors,
        usage=usage,
        question=question,
    )
