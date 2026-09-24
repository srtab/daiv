from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.messages import BaseMessage
    from langgraph.types import StateSnapshot

    from automation.agent.results import AgentResult
    from codebase.base import Issue, MergeRequest, Scope
    from codebase.references import ExternalRef
    from sessions.executor.lock import LockPolicy


@dataclass(frozen=True, kw_only=True)
class RunSpec:
    """What a trigger decided about one agent run.

    ``persist_ref`` and ``arm_watch`` are opt-in because each writes state outside the checkpoint: the
    session's working branch, and a CI watch on the merge request. ``run_id`` names the ``Run`` row the
    resolved model is recorded on, together with its session. ``fallback_ref_on_missing`` lets the clone
    degrade to the default branch when ``ref`` is gone; the session is then re-pinned to where it landed.
    ``use_max`` picks the site's max model (the ``daiv-max`` label). ``recover_draft`` publishes a draft
    merge request from the checkpoint when the agent raises.
    """

    thread_id: str
    repo_id: str
    scope: Scope
    input_messages: tuple[BaseMessage, ...]
    trigger: str
    lock: LockPolicy
    ref: str | None = None
    issue: Issue | None = None
    merge_request: MergeRequest | None = None
    fallback_ref_on_missing: bool = False
    agent_model: str | None = None
    agent_thinking_level: str | None = None
    use_max: bool = False
    sandbox_env_id: str | None = None
    acting_user_id: int | None = None
    mcp_overrides: dict[str, str] = field(default_factory=dict)
    references: tuple[ExternalRef, ...] = ()
    run_id: str | None = None
    persist_ref: bool = False
    arm_watch: bool = False
    recover_draft: bool = False
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class RunOutcome:
    """``snapshot`` is ``None`` when the post-run checkpoint read failed; the run itself still succeeded."""

    agent_result: AgentResult
    response_text: str
    snapshot: StateSnapshot | None


class FailureHook(Protocol):
    """``draft_published`` says whether draft recovery published a draft after the error, and ``snapshot`` is the
    state it re-read afterwards; ``False`` and ``None`` when no recovery ran."""

    async def __call__(self, exc: Exception, /, *, draft_published: bool, snapshot: StateSnapshot | None) -> None: ...


@dataclass(frozen=True, kw_only=True)
class RunHooks:
    """Trigger callbacks, awaited after the run's context closes and while the session slot is still held.

    ``on_failure`` sees every ``Exception`` from the lock step through closing the context (a cancellation skips
    it), and the executor re-raises once it returns. Any lock-step error — a ``SessionLockTimeoutError`` or
    another failure inside the claim — runs without the slot, which was never claimed. An error ``on_failure``
    raises is logged; it never replaces the run's own. An error from ``on_success`` propagates as-is and never
    reaches ``on_failure``.
    """

    on_success: Callable[[RunOutcome], Awaitable[None]] | None = None
    on_failure: FailureHook | None = None
