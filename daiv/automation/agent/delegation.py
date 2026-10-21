"""What the ``delegate_jobs`` tool needs from whatever starts the sub-jobs it delegates.

The agent starts nothing itself: the run executor hands ``create_daiv_agent`` a :class:`JobDelegator`
(``sessions.delegation.SessionJobDelegator``), and without one the agent has no ``delegate_jobs`` tool.
Standard library only, like ``automation.agent.artifacts``, so ``sessions`` can import it at ``django.setup()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from automation.agent.references import RefIn

DELEGATE_JOBS_TOOL_NAME = "delegate_jobs"
MAX_DELEGATED_TARGETS = 10


class DelegationError(ValueError):
    """The whole call was refused and nothing started; ``str()`` says why, for the agent to act on."""


@dataclass(frozen=True)
class Checkout:
    """The delegating run's clone: ``ref`` is the branch it checked out, ``default_branch`` its repository's."""

    repo_id: str
    ref: str
    default_branch: str


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
    repo_id: str
    ref: str
    error: str


@dataclass(frozen=True)
class DelegationResult:
    """``batch_id`` is ``None`` when every target was refused before a batch was made; a batch whose legs all failed
    to start still has one, so ``started`` is what says whether anything runs."""

    batch_id: str | None
    started: list[StartedLeg] = field(default_factory=list)
    failed: list[FailedLeg] = field(default_factory=list)


class JobDelegator(Protocol):
    async def adelegate(self, *, checkout: Checkout, goal: str, targets: Sequence[DelegatedTarget]) -> DelegationResult:
        """Start one independent job per target for the delegating run, which has ``checkout``, and return without
        waiting. Each leg publishes to a new branch and merge request of its own, and one on ``checkout``'s own branch
        starts once the delegating run has finished.

        Once every started leg has finished, the session is resumed with a run whose prompt summarises them.
        A target that cannot start lands in ``failed`` while the others go ahead; raise :class:`DelegationError`
        to refuse the whole call.
        """
        ...
