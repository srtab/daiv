from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, TypedDict

from django.template.loader import render_to_string

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
    is_gitlab: bool,
) -> AgentResult:
    """Build a standardized :class:`AgentResult` from the agent's persisted state.

    ``code_changes`` is a PrivateStateAttr, so it's omitted from ainvoke output.
    We read it from the persisted checkpoint instead. Callers that have already
    read the state can pass a pre-fetched ``snapshot`` to avoid a redundant Redis
    round-trip; passing ``None`` explicitly signals that the read already failed
    (so we don't silently retry it here and risk drifting from whatever the
    caller decided based on the same failure).

    ``is_gitlab`` only picks the merge-request/pull-request wording for the owed-branch notice, and has
    no default for the same reason :func:`render_pending_branch_notice` doesn't: a default would silently
    hand one platform the other's vocabulary at whichever call site forgot it.
    """
    if snapshot is NO_SNAPSHOT:
        snapshot = await agent.aget_state(config=config)
    if snapshot is None:
        return AgentResult(
            response=response, code_changes=False, merge_request_id=None, merge_request_web_url=None, usage=usage
        )
    mr = snapshot.values.get("merge_request")
    # An MR in state wins: it is the concrete destination to point the caller at, so the pending
    # notice is only considered when there is none. (The middleware clears the pending branch when it
    # records an MR and vice versa, but recovery paths write state directly, so this is a precedence
    # rule rather than an invariant the reader can rely on.)
    return AgentResult(
        response=response if mr else _with_pending_branch_notice(response, snapshot, is_gitlab=is_gitlab),
        code_changes=bool(snapshot.values.get("code_changes")),
        merge_request_id=mr.merge_request_id if mr else None,
        merge_request_web_url=mr.web_url if mr else None,
        usage=usage,
    )


def render_pending_branch_notice(snapshot: Any, *, is_gitlab: bool) -> str | None:
    """The "pushed, but no merge request yet" notice for a run that left one owed, or ``None``.

    Split out from :func:`build_agent_result` because that function only feeds the *job result*. Issue-
    and MR-scope runs post their reply to the platform straight from the agent's last message, so
    without a separately renderable notice the person who triggered the run sees a normal reply with no
    MR link and no explanation — indistinguishable from a run that changed nothing.

    The wording depends on ``pending_mr_branch_verified``: only a branch actually confirmed on the
    remote may be described as safely holding the work. It also absorbs the protected-branch explanation
    when both applied, because the dedicated protected-branch footer links the replacement MR by URL and
    on this path that MR does not exist yet — leaving the reader with a branch they were never told why
    they got.

    ``is_gitlab`` has no default on purpose: the wording says "merge request" or "pull request", and a
    default would silently give one platform the other's vocabulary at whichever call site forgot it.
    """
    if snapshot is None:
        return None
    branch = snapshot.values.get("pending_mr_branch")
    if not branch:
        return None
    return render_to_string(
        "automation/pending_merge_request.txt",
        {
            "branch": branch,
            "verified": bool(snapshot.values.get("pending_mr_branch_verified")),
            "protected_source_branch": snapshot.values.get("protected_branch_fallback_source"),
            "is_gitlab": is_gitlab,
        },
    )


def append_footer(body: str, footer: str | None) -> str:
    """Attach a footer below a reply body, separated by a blank line.

    The blank line is required, not cosmetic: footers open with a ``---`` rule, and Markdown reads
    ``---`` on the line directly after text as a setext heading underline — which swallows the rule and
    turns the preceding sentence into a heading.

    Lives here rather than on ``BaseManager`` because the import edge is one-way: ``codebase.managers``
    imports this module, nothing in ``automation`` imports the managers. One implementation means a fix
    to the rule can't miss half its call sites.
    """
    if not footer:
        return body
    return f"{body.rstrip()}\n\n{footer.strip()}"


def _with_pending_branch_notice(response: str, snapshot: Any, *, is_gitlab: bool) -> str:
    """Append the owed-merge-request notice to a job result's response text."""
    notice = render_pending_branch_notice(snapshot, is_gitlab=is_gitlab)
    if not notice:
        return response
    if response:
        return append_footer(response, notice)
    # The template leads with a horizontal rule to separate itself from a reply. Standing alone there
    # is nothing above it to separate, so the rule would just open the message with a stray line.
    return notice.strip().removeprefix("---").lstrip()
