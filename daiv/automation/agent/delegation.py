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
    """One sub-job: ``ref`` blank for the repository's default branch."""

    repo_id: str
    ref: str
    prompt: str


@dataclass(frozen=True)
class StartedLeg:
    repo_id: str
    ref: str
    thread_id: str
    session_url: str


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
    async def adelegate(
        self, *, thread_id: str, checkout: Checkout, goal: str, targets: Sequence[DelegatedTarget]
    ) -> DelegationResult:
        """Start one independent job per target for the session ``thread_id``, whose run has ``checkout``, and return
        without waiting.

        Once every started leg has finished, the session is resumed with a run whose prompt summarises them.
        A target that cannot start lands in ``failed`` while the others go ahead; raise :class:`DelegationError`
        to refuse the whole call.
        """
        ...
