from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from sessions.executor.lock import NoLock

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
    merge request from the checkpoint when the agent raises. ``input_messages`` is the agent's input for
    ``execute_run``; ``stream_run`` leaves the input to its stream factory, so a streaming trigger passes ``()``.

    ``thread_id=None`` is a one-shot run (evals): ``NoLock``, an in-memory checkpoint, no session switches.
    ``model_names`` is the exact chain, unresolved; ``agent_thinking_level`` then goes as given (``None``: no thinking).
    ``context_options`` / ``agent_options`` are extra kwargs for ``set_runtime_ctx`` / ``create_daiv_agent``.
    """

    thread_id: str | None
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
    model_names: tuple[str, ...] = ()
    context_options: dict[str, Any] = field(default_factory=dict)
    agent_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.thread_id == "":
            raise ValueError("a session run needs a non-empty thread_id")
        if self.model_names and (self.agent_model or self.use_max):
            raise ValueError("model_names is the exact model chain; it takes neither agent_model nor use_max")
        if self.thread_id is None and (
            not isinstance(self.lock, NoLock)
            or self.run_id is not None
            or self.persist_ref
            or self.arm_watch
            or self.recover_draft
            or self.fallback_ref_on_missing
        ):
            raise ValueError("a one-shot run (thread_id=None) has no session to lock, record, sync, arm or recover")


@dataclass(frozen=True, kw_only=True)
class RunOutcome:
    """``response_text`` is the agent's last message, read from the checkpoint for a stream. ``snapshot`` is ``None``
    when the post-run checkpoint read failed; the run itself still succeeded."""

    agent_result: AgentResult
    response_text: str
    snapshot: StateSnapshot | None


class FailureHook(Protocol):
    """``draft_published`` says whether draft recovery published a draft after the error, and ``snapshot`` is the
    state it re-read afterwards; ``False`` and ``None`` when no recovery ran."""

    async def __call__(self, exc: Exception, /, *, draft_published: bool, snapshot: StateSnapshot | None) -> None: ...


@dataclass(frozen=True, kw_only=True)
class RunHooks:
    """Trigger callbacks.

    ``on_context_ready(ref)`` is awaited inside the run once the clone is ready and any fallback re-pin is done,
    before the model is resolved; ``ref`` is the ref the clone landed on. An error it raises fails the run as a
    setup error.

    ``on_success`` and ``on_failure`` are awaited after the run's context closes and while the session slot is still
    held. ``on_failure`` sees every ``Exception`` from the lock step through closing the context (a cancellation
    skips it), and the executor re-raises once it returns. Any lock-step error — a ``SessionLockTimeoutError`` or
    another failure inside the claim — runs without the slot, which was never claimed. An error ``on_failure``
    raises is logged; it never replaces the run's own. An error from ``on_success`` propagates as-is and never
    reaches ``on_failure``.
    """

    on_context_ready: Callable[[str], Awaitable[None]] | None = None
    on_success: Callable[[RunOutcome], Awaitable[None]] | None = None
    on_failure: FailureHook | None = None
