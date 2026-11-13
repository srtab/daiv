"""The checks a run request passes before a trigger submits it.

The jobs API, MCP ``submit_job`` and ``schedule_job``, chat, the run and schedule forms and the agent's
``delegate_jobs`` take their admission checks from this module: the agent override, WRITE access on the repositories
and the named sandbox environment. Each check re-raises its failure as an :class:`AdmissionError` subclass whose
``str()`` is the user-facing message, and each trigger maps that one family to its transport (an HTTP status, an MCP
error string, a form field).
:func:`sessions.services.asubmit_batch_runs` repeats the access check at submit time through this module too, so
a denial is a :class:`RepoAccessError` wherever it is caught.

:func:`aadmit` runs the whole sequence for a batch submitted now, in this order: override, repository access,
environment, the per-repository environment of each target, then the MCP server overrides that the jobs API and MCP
``submit_job`` take. Triggers that need a different sequence call the individual checks: MCP ``schedule_job`` stores
the request instead of submitting it, ``delegate_jobs`` (``sessions.delegation``) fails only the targets it is denied
and names no environment, and the run and schedule forms report each failure on its own field and take the
environment from a model choice field, not a name. Chat calls only the access and environment checks and diffs its
own MCP server selection. It validates the agent override with :mod:`automation.agent.validators` itself, because
the override that runs is the one pinned to its session and a stored override that no longer validates gets its own
message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from mcp_connectors.selection import build_selection_pool, overrides_from_switches
from sandbox_envs.selection import aresolve_repo_envs, resolve_env_for_user

from automation.agent import validators
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied, assert_can_run
from sessions.models import Session

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
    """The caller lacks WRITE access on ``repo_ids``. The message never names them, so a 404 stays opaque."""

    def __init__(self, repo_ids: list[str]):
        self.repo_ids = repo_ids
        super().__init__(REPO_ACCESS_DENIED_MESSAGE)


class UnknownEnvironmentError(AdmissionError):
    """The named sandbox environment is not one the caller can see."""


class InvalidMcpOverridesError(AdmissionError):
    """An MCP server override names a server the caller cannot use."""


@dataclass(frozen=True)
class Admission:
    """A request :func:`aadmit` accepted: the normalised overrides, and each target with its environment resolved."""

    agent_model: str
    agent_thinking_level: str
    targets: list[RepoTarget]
    mcp_overrides: dict[str, str] | None


def check_agent_override(agent_model: str | None, agent_thinking_level: str | None) -> tuple[str, str]:
    """Return the normalised ``(agent_model, agent_thinking_level)``, refusing a request no model can serve."""
    try:
        model, level = validators.validate_agent_override(agent_model, agent_thinking_level)
        validators.ensure_agent_model_available(model)
    except validators.AgentOverrideError as err:
        raise RejectedOverrideError(str(err)) from err
    return model, level


def assert_repo_access(user: User, repo_ids: Iterable[str]) -> None:
    try:
        assert_can_run(user, repo_ids)
    except RepositoryAccessDenied as err:
        raise RepoAccessError(err.repo_ids) from err


aassert_repo_access = sync_to_async(assert_repo_access)


async def aresolve_environment(user: User, name_or_id: str | None) -> SandboxEnvironment | None:
    """The environment ``name_or_id`` names, or ``None`` when it is empty."""
    try:
        return await resolve_env_for_user(user, name_or_id)
    except LookupError as err:
        raise UnknownEnvironmentError(str(err)) from err


async def aresolve_mcp_overrides(
    user: User, switches: dict[str, bool] | None, thread_id: str | None = None
) -> dict[str, str] | None:
    """``switches`` applied to ``thread_id``'s MCP overrides against the caller's servers, or ``None`` when none are
    sent: a server they don't name keeps its setting."""
    if not switches:
        return None
    current = None
    if thread_id is not None:
        current = await Session.objects.filter(thread_id=thread_id).values_list("mcp_overrides", flat=True).afirst()
    try:
        return overrides_from_switches(switches, await sync_to_async(build_selection_pool)(user.pk), current)
    except ValueError as err:
        raise InvalidMcpOverridesError(str(err)) from err


async def aadmit(
    *,
    user: User,
    repos: list[RepoTarget],
    agent_model: str | None,
    agent_thinking_level: str | None,
    environment: str | None,
    mcp_overrides: dict[str, bool] | None = None,
    thread_id: str | None = None,
) -> Admission:
    """Run every admission check for a batch submitted now; ``environment`` names one env for all of ``repos``."""
    model, level = check_agent_override(agent_model, agent_thinking_level)
    await aassert_repo_access(user, [target.repo_id for target in repos])
    env = await aresolve_environment(user, environment)
    targets = await aresolve_repo_envs(user=user, repos=repos, explicit_env_id=str(env.id) if env else None)
    return Admission(
        agent_model=model,
        agent_thinking_level=level,
        targets=targets,
        mcp_overrides=await aresolve_mcp_overrides(user, mcp_overrides, thread_id),
    )
