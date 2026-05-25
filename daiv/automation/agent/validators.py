from __future__ import annotations

from automation.agent.base import parse_model_spec
from core.models import ThinkingLevelChoices
from core.site_settings import site_settings


class AgentOverrideError(ValueError):
    """Raised when the user-supplied agent override is invalid.

    Carries the same message text across API/MCP/form surfaces so callers can
    surface it verbatim without per-layer rephrasing.
    """


class AgentConfigurationError(RuntimeError):
    """Raised when no agent model can be resolved for a run.

    Fires from :func:`automation.agent.utils.get_daiv_agent_kwargs` when the
    caller passes no ``agent_model`` override AND ``site_settings.agent_model_name``
    is also empty. Surfaces from MCP / API / webhook layers so an admin can see
    why a run was refused (instead of the request silently using a stale
    repo-config fallback).
    """


_VALID_LEVELS = frozenset(ThinkingLevelChoices.values)


def validate_agent_override(agent_model: str | None, agent_thinking_level: str | None) -> tuple[str, str]:
    """Validate the per-run model override pair.

    Returns the normalised ``(agent_model, agent_thinking_level)`` strings: empty
    string when the input is ``None`` or empty. ``agent_model`` is parsed against
    the live ``Provider`` table and rejected when its provider row is disabled —
    catching the case at submit time rather than blowing up inside
    :func:`automation.agent.base.BaseAgent.get_model_kwargs` mid-run.
    ``agent_thinking_level`` is checked against ``ThinkingLevelChoices``. Either
    field may be empty independently — setting thinking without a model overrides
    effort on the auto-resolved model.
    """
    model = (agent_model or "").strip()
    level = (agent_thinking_level or "").strip()

    if model:
        try:
            resolved = parse_model_spec(model)
        except ValueError as err:
            raise AgentOverrideError(str(err)) from err
        if not resolved.row.is_enabled:
            raise AgentOverrideError(
                f"Provider '{resolved.row.slug}' is disabled. Enable it or pick a model from another provider."
            )

    if level and level not in _VALID_LEVELS:
        raise AgentOverrideError(f"Invalid thinking level '{level}'. Valid: {', '.join(sorted(_VALID_LEVELS))}.")

    return model, level


_NO_MODEL_AVAILABLE_MSG = (
    "No agent model specified and no system default is configured. "
    "Pass `agent_model` explicitly, or ask an administrator to set the system default."
)


def ensure_agent_model_available(agent_model: str) -> None:
    """Submit-time check: refuse to enqueue work when no model can be resolved.

    The override layer (:func:`validate_agent_override`) only validates that a
    *provided* value is well-formed. This helper enforces that a model is *available*
    at all — i.e. the caller supplied one OR the admin configured a system default.
    Both reduce to the same end state in :func:`automation.agent.utils.get_daiv_agent_kwargs`,
    but raising at submit time gives MCP / API callers a clear 4xx-style refusal
    instead of a deferred failure inside the async agent kickoff.
    """
    if agent_model:
        return
    if not site_settings.agent_model_name:
        raise AgentOverrideError(_NO_MODEL_AVAILABLE_MSG)
