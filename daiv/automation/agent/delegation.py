"""What the ``delegate_jobs`` tools need from whatever starts, follows and cancels the sub-jobs they delegate.

The agent starts nothing itself: the run executor hands ``create_daiv_agent`` a :class:`JobDelegator`
(``sessions.delegation.SessionJobDelegator``), and without one the agent has none of the tools.
Standard library only, like ``automation.agent.artifacts``, so ``sessions`` can import it at ``django.setup()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from automation.agent.references import RefIn

DELEGATE_JOBS_TOOL_NAME = "delegate_jobs"
CHECK_DELEGATED_JOBS_TOOL_NAME = "check_delegated_jobs"
CANCEL_DELEGATED_JOBS_TOOL_NAME = "cancel_delegated_jobs"
MAX_DELEGATED_TARGETS = 10


class DelegationError(ValueError):
    """The whole call was refused and nothing started; ``str()`` says why, for the agent to act on."""


@dataclass(frozen=True)
class Checkout:
    """The delegating run's clone: ``ref`` is the branch it checked out."""

    repo_id: str
    ref: str


@dataclass(frozen=True)
class DelegatedTarget:
    """One sub-job: ``ref`` blank for the repository's default branch; ``references`` are linked into its merge
    request."""

    repo_id: str
    ref: str
    prompt: str
    references: tuple[RefIn, ...] = ()


@dataclass(frozen=True)
class StartedLeg:
    """``starts_after_this_turn``: the leg branches from the delegating run's own branch, so it starts once that run
    has finished and pushed."""

    repo_id: str
    ref: str
    thread_id: str
    session_url: str
    starts_after_this_turn: bool = False


@dataclass(frozen=True)
class FailedLeg:
    """``still_runs``: the leg started but cannot be followed, so it may still publish while no resume reports it."""

    repo_id: str
    ref: str
    error: str
    still_runs: bool = False


@dataclass(frozen=True)
class DelegationResult:
    """``batch_id`` is ``None`` when every target was refused before a batch was made; a batch whose legs all failed
    to start still has one, so ``started`` is what says whether anything runs."""

    batch_id: str | None
    started: list[StartedLeg] = field(default_factory=list)
    failed: list[FailedLeg] = field(default_factory=list)


@dataclass(frozen=True)
class LegStatus:
    """A leg as its coordinator sees it: ``branch`` is the branch it publishes to, once that is not ``ref``, and
    ``reply`` its reply, cut short."""

    repo_id: str
    ref: str
    thread_id: str
    session_url: str
    status: str
    branch: str = ""
    merge_request_url: str = ""
    reply: str = ""


@dataclass(frozen=True)
class BatchStatus:
    """``finished``: every leg has ended, so nothing in the batch runs any more."""

    batch_id: str
    finished: bool
    legs: list[LegStatus]


@dataclass(frozen=True)
class CancelResult:
    """``cancelled`` holds the thread ids of the legs this call cancelled, and ``batch`` the batch after it;
    ``resumes`` says whether the batch still resumes the delegating session."""

    batch: BatchStatus
    cancelled: list[str]
    resumes: bool


class JobDelegator(Protocol):
    async def adelegate(self, *, checkout: Checkout, goal: str, targets: Sequence[DelegatedTarget]) -> DelegationResult:
        """Start one independent job per target for the delegating run, which has ``checkout``, and return without
        waiting. Each leg publishes to a new branch and merge request of its own, and one that names ``checkout``'s
        own branch starts once the delegating run has finished, from the branch that run's session then works on.

        Once every started leg has finished, the session is resumed with a run whose prompt summarises them.
        A target that cannot start lands in ``failed`` while the others go ahead; raise :class:`DelegationError`
        to refuse the whole call.
        """
        ...

    async def astatus(self, *, batch_id: str | None) -> list[BatchStatus]:
        """The batches this session delegated, newest first: ``batch_id``'s alone, or the latest few without one.

        Raise :class:`DelegationError` for a ``batch_id`` this session did not delegate.
        """
        ...

    async def acancel(self, *, batch_id: str, thread_ids: Sequence[str]) -> CancelResult:
        """Cancel the unfinished legs of ``batch_id``, only those in ``thread_ids`` when it is not empty; a running leg
        stops within seconds.

        Raise :class:`DelegationError`, cancelling nothing, for a batch or a leg this session did not delegate.
        """
        ...
