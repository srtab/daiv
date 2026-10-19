"""The checks a run request passes before a trigger submits it.

The jobs API, MCP ``submit_job`` and ``schedule_job``, chat and the run and schedule forms take their admission
checks from this module: the agent override, WRITE access on the repositories and the named sandbox environment.
Each check re-raises its failure as an :class:`AdmissionError` subclass whose ``str()`` is the user-facing message,
and each trigger maps that one family to its transport (an HTTP status, an MCP error string, a form field).

:func:`aadmit` runs the whole sequence for a batch submitted now, in this order: override, repository access,
environment, then the per-repository environment of each target. Triggers that need a different sequence call
the individual checks: MCP ``schedule_job`` stores the request instead of submitting it, and the run and schedule
forms report each failure on its own field and take the environment from a model choice field, not a name.
Chat calls only the access and environment checks; it validates the override with
:mod:`automation.agent.validators` itself, because the override that runs is the one pinned to its session and a
stored override that no longer validates gets its own message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sandbox_envs.selection import aresolve_repo_envs, resolve_env_for_user

from automation.agent.validators import AgentOverrideError, ensure_agent_model_available, validate_agent_override
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied, aassert_can_run, assert_can_run

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sandbox_envs.models import SandboxEnvironment

    from accounts.models import User
    from sessions.services import RepoTarget


class AdmissionError(Exception):
    """A run request was refused; ``str(err)`` is the message to show the caller."""


class RejectedOverrideError(AdmissionError):
    """The model or thinking-level override is invalid, or no model can be resolved for the run."""


class RepoAccessError(AdmissionError):
    """The caller lacks WRITE access on a repository. The message is the same whichever repository it was."""


class UnknownEnvironmentError(AdmissionError):
    """The named sandbox environment is not one the caller can see."""


@dataclass(frozen=True)
class Admission:
    """A request :func:`aadmit` accepted: the normalised override, and each target with its environment resolved."""

    agent_model: str
    agent_thinking_level: str
    targets: list[RepoTarget]


def check_agent_override(agent_model: str | None, agent_thinking_level: str | None) -> tuple[str, str]:
    """Return the normalised ``(agent_model, agent_thinking_level)``, refusing a request no model can serve."""
    try:
        model, level = validate_agent_override(agent_model, agent_thinking_level)
        ensure_agent_model_available(model)
    except AgentOverrideError as err:
        raise RejectedOverrideError(str(err)) from err
    return model, level


def assert_repo_access(user: User, repo_ids: Iterable[str]) -> None:
    try:
        assert_can_run(user, repo_ids)
    except RepositoryAccessDenied as err:
        raise RepoAccessError(REPO_ACCESS_DENIED_MESSAGE) from err


async def aassert_repo_access(user: User, repo_ids: Iterable[str]) -> None:
    try:
        await aassert_can_run(user, repo_ids)
    except RepositoryAccessDenied as err:
        raise RepoAccessError(REPO_ACCESS_DENIED_MESSAGE) from err


async def aresolve_environment(user: User, name_or_id: str | None) -> SandboxEnvironment | None:
    """The environment ``name_or_id`` names, or ``None`` when it is empty."""
    try:
        return await resolve_env_for_user(user, name_or_id)
    except LookupError as err:
        raise UnknownEnvironmentError(str(err)) from err


async def aadmit(
    *,
    user: User,
    repos: list[RepoTarget],
    agent_model: str | None,
    agent_thinking_level: str | None,
    environment: str | None,
) -> Admission:
    """Run every admission check for a batch submitted now; ``environment`` names one env for all of ``repos``."""
    model, level = check_agent_override(agent_model, agent_thinking_level)
    await aassert_repo_access(user, [target.repo_id for target in repos])
    env = await aresolve_environment(user, environment)
    targets = await aresolve_repo_envs(user=user, repos=repos, explicit_env_id=str(env.id) if env else None)
    return Admission(agent_model=model, agent_thinking_level=level, targets=targets)
